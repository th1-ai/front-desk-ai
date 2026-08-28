"""Tests for tools/coach.py - clustering, propose/accept/apply, never auto-applies."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.config import load_settings
from core.llm import LLMPendingInteractive
from core.review import edit
from core.store import Store

import coach
import store_ext


def _settings():
    return load_settings(provider="mock", mode="shadow")


def _args(**kw):
    return SimpleNamespace(**kw)


def _make_edit(store, n, intent="question"):
    item = store.upsert_item("email", f"learn-{n}", kind="message", payload={"body": "hi"})
    store.set_fields(item.id, intent=intent, draft={"subject": "x", "body": "Old body"})
    store.transition(item.id, "pending_review")
    edit(store, item.id, {"subject": "x", "body": "New body"}, note="tone was too formal")
    return item


def test_cluster_learnings_needs_the_configured_minimum():
    learnings = [{"applied_to": "question", "ts": "t1", "before": "a", "after": "b"}]
    assert coach.cluster_learnings(learnings, min_cluster_size=2) == []
    learnings.append({"applied_to": "question", "ts": "t2", "before": "c", "after": "d"})
    clusters = coach.cluster_learnings(learnings, min_cluster_size=2)
    assert len(clusters) == 1
    assert clusters[0]["cluster_size"] == 2


def test_analyze_creates_a_pending_proposal_from_two_similar_edits(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "coach1.db")
    store_ext.ensure_schema(store)
    _make_edit(store, 1)
    _make_edit(store, 2)

    rc = coach.cmd_analyze(store, settings, _args(provider=None))
    assert rc == 0
    pending = store.db.execute(
        "SELECT * FROM coach_proposals WHERE status='pending'").fetchall()
    assert len(pending) == 1
    assert pending[0]["cluster_size"] == 2
    store.close()


def test_a_single_edit_is_not_enough_to_form_a_cluster(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "coach2.db")
    store_ext.ensure_schema(store)
    _make_edit(store, 1)
    coach.cmd_analyze(store, settings, _args(provider=None))
    assert store.db.execute("SELECT COUNT(*) AS n FROM coach_proposals").fetchone()["n"] == 0
    store.close()


def test_a_rejected_proposal_never_reaches_knowledge_rules(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "coach3.db")
    store_ext.ensure_schema(store)
    _make_edit(store, 1)
    _make_edit(store, 2)
    coach.cmd_analyze(store, settings, _args(provider=None))
    row = store.db.execute("SELECT id FROM coach_proposals LIMIT 1").fetchone()
    coach.cmd_reject(store, _args(id=row["id"], note="not a real pattern"))
    applied = store.db.execute(
        "SELECT COUNT(*) AS n FROM coach_proposals WHERE status='applied'").fetchone()["n"]
    assert applied == 0
    store.close()


def test_accept_then_apply_writes_one_line_to_knowledge_rules(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "coach4.db")
    store_ext.ensure_schema(store)
    _make_edit(store, 1)
    _make_edit(store, 2)
    coach.cmd_analyze(store, settings, _args(provider=None))
    row = store.db.execute("SELECT id FROM coach_proposals LIMIT 1").fetchone()

    was_present = coach.RULES_FILE.exists()
    before = coach.RULES_FILE.read_text(encoding="utf-8") if was_present else ""
    try:
        coach.cmd_accept(store, _args(id=row["id"], note=""))
        coach.cmd_apply(store, _args())
        assert coach.RULES_FILE.exists()
        text = coach.RULES_FILE.read_text(encoding="utf-8")
        assert text.count("\n- (") >= 1
        status = store.db.execute("SELECT status FROM coach_proposals WHERE id=?",
                                  (row["id"],)).fetchone()["status"]
        assert status == "applied"
    finally:
        if was_present:
            coach.RULES_FILE.write_text(before, encoding="utf-8")
        else:
            coach.RULES_FILE.unlink(missing_ok=True)
    store.close()


def test_interactive_coach_suggestion_round_trip_writes_a_clean_line(tmp_path):
    """Regression test for SIMULATION.md finding 3. The `interactive`
    provider's own generated prompt tells a human to answer as JSON; without
    a schema wired through `complete()`, that JSON was never parsed back out
    and a raw `{"text": "..."}` blob landed in knowledge/rules.md. With
    prompts/schemas/coach-suggestion.json in place, the answer is parsed and
    validated like every other reasoning step, and only the clean `text`
    value is ever stored or written."""
    settings = load_settings(provider="interactive", mode="shadow")
    store = Store(settings, path=tmp_path / "coach-interactive.db")
    store_ext.ensure_schema(store)
    _make_edit(store, 1)
    _make_edit(store, 2)

    pending_dir = settings.root / "data" / "pending"
    for leftover in pending_dir.glob("*coach-suggestion*"):
        leftover.unlink()
    try:
        try:
            coach.cmd_analyze(store, settings, _args(provider=None))
            assert False, "expected LLMPendingInteractive"
        except LLMPendingInteractive as exc:
            answer_path = exc.answer_path
            assert exc.schema_path is not None   # a schema was actually offered

        answer_path.write_text(json.dumps({"text": "Add a line to knowledge/policies.md."}),
                               encoding="utf-8")
        rc = coach.cmd_analyze(store, settings, _args(provider=None))
        assert rc == 0

        row = store.db.execute(
            "SELECT * FROM coach_proposals WHERE status='pending'").fetchone()
        assert row is not None
        assert row["suggested_fix"] == "Add a line to knowledge/policies.md."
        assert "{" not in row["suggested_fix"] and "}" not in row["suggested_fix"]

        coach.cmd_accept(store, _args(id=row["id"], note=""))
        was_present = coach.RULES_FILE.exists()
        before = coach.RULES_FILE.read_text(encoding="utf-8") if was_present else ""
        try:
            coach.cmd_apply(store, _args())
            text = coach.RULES_FILE.read_text(encoding="utf-8")
            new_line = text.splitlines()[-1]
            assert new_line == "- (question) Add a line to knowledge/policies.md."
        finally:
            if was_present:
                coach.RULES_FILE.write_text(before, encoding="utf-8")
            else:
                coach.RULES_FILE.unlink(missing_ok=True)
    finally:
        for leftover in pending_dir.glob("*coach-suggestion*"):
            leftover.unlink()
        store.close()
