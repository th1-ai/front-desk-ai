#!/usr/bin/env python3
"""tools/coach.py - Email Optimizer / Coach AI: learn from human corrections.

    python3 tools/coach.py analyze     # cluster edits + rejections into proposals;
                                       # draft one suggestion per resolved escalation
                                       # that does not have one yet
    python3 tools/coach.py list        # pending proposals waiting on a human
    python3 tools/coach.py show <id>
    python3 tools/coach.py accept <id> [--note "..."]
    python3 tools/coach.py reject <id> [--note "..."]
    python3 tools/coach.py apply       # write accepted proposals into knowledge/rules.md

Two sources, one rule: never touches a guest, never changes behaviour on its
own. See docs/how-it-works.md ("Email Optimizer / Coach AI") and
workflows/85-coach-weekly.md.

**Learnings** (``core.store``'s ``learnings`` table, written automatically by
``core.review.edit()`` and ``core.review.reject()``) are grouped by the intent
or kind they happened on. A group at or above ``coach.min_cluster_size``
(config/agent.yaml, default 2) is a real pattern, not noise, and gets ONE
model call to turn it into a concrete suggestion - stored as a row in
``coach_proposals`` (tools/store_ext.py) with ``status='pending'``.

**Resolved escalations** missing an ``improvement_suggestion``
(``store_ext.resolved_escalations_missing_suggestion``) each get one targeted
suggestion too, written straight onto the escalation row
(``store_ext.set_improvement_suggestion``) - advisory text for whoever reviews
escalations next, not a proposal a human has to accept separately.

A proposal only ever changes anything after a human runs ``accept`` and then
``apply`` - a rejected or still-pending proposal changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive, complete  # noqa: E402
from core.store import Store, StoreError, utcnow  # noqa: E402
from core.templates import build_prompt  # noqa: E402

import store_ext  # noqa: E402

KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
RULES_FILE = KNOWLEDGE_DIR / "rules.md"
SCHEMAS_DIR = REPO_ROOT / "prompts" / "schemas"
#: {"text": "..."} - see prompts/coach-suggestion.md and SIMULATION.md finding 3.
#: Without a schema, the `interactive` provider's own generated instructions
#: ("write the result... as JSON with a single key `text`") produce a raw
#: JSON blob that nothing ever parses back out - it was going straight into
#: knowledge/rules.md unparsed. A schema makes `core.llm` parse and validate
#: the answer like every other reasoning step in this repo.
COACH_SUGGESTION_SCHEMA = json.loads(
    (SCHEMAS_DIR / "coach-suggestion.json").read_text(encoding="utf-8"))


def _suggest(settings: Settings, store: Store, *, pattern: str, intent: str,
            cluster_size: int, example_before: str, example_after: str,
            provider: str | None) -> str:
    prompt = build_prompt("coach-suggestion", settings=settings, pattern=pattern,
                          intent=intent, cluster_size=cluster_size,
                          example_before=example_before[:500],
                          example_after=example_after[:500])
    result = complete("coach-suggestion", prompt, COACH_SUGGESTION_SCHEMA, settings=settings,
                      provider=provider, store=store)
    return str((result.data or {}).get("text", "")).strip()


def cluster_learnings(learnings: list[dict], min_cluster_size: int) -> list[dict]:
    """Group edits/rejections by the intent or kind they happened on.

    Deterministic on purpose - no fuzzy text matching, so this is trivial to
    test and to explain. ``applied_to`` is set by ``core.review.edit()`` /
    ``reject()`` to ``item.intent or item.kind``. A group below the threshold
    is real but too small to be a pattern yet; it stays in ``learnings`` for
    next week's run to pick up.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in learnings:
        key = row.get("applied_to") or "unknown"
        groups[key].append(row)
    clusters = []
    for key, rows in groups.items():
        if len(rows) < min_cluster_size:
            continue
        rows.sort(key=lambda r: r["ts"])
        latest = rows[-1]
        clusters.append({
            "pattern": key, "intent": key, "cluster_size": len(rows),
            "example_before": latest.get("before") or "",
            "example_after": latest.get("after") or "",
        })
    return clusters


def cmd_analyze(store: Store, settings: Settings, args) -> int:
    min_size = int(settings.agent_get("coach.min_cluster_size", 2))
    learnings = store.list_learnings(limit=500)
    clusters = cluster_learnings(learnings, min_size)
    created = 0
    for c in clusters:
        suggestion = _suggest(settings, store, pattern=c["pattern"], intent=c["intent"],
                              cluster_size=c["cluster_size"],
                              example_before=c["example_before"],
                              example_after=c["example_after"], provider=args.provider)
        store.db.execute(
            "INSERT INTO coach_proposals (id, created_at, pattern, intent, cluster_size, "
            "example_before, example_after, suggested_fix, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), utcnow(), c["pattern"], c["intent"], c["cluster_size"],
             c["example_before"][:2000], c["example_after"][:2000], suggestion, "pending"))
        created += 1

    suggested = 0
    for row in store_ext.resolved_escalations_missing_suggestion(store):
        suggestion = _suggest(settings, store, pattern="resolved escalation",
                              intent=row.get("category", ""), cluster_size=1,
                              example_before=row.get("ai_suggested_reply") or "",
                              example_after=row.get("resolution_note") or "",
                              provider=args.provider)
        store_ext.set_improvement_suggestion(store, row["id"], suggestion)
        suggested += 1

    print(f"{created} proposal(s) from {len(clusters)} cluster(s) of "
         f"{len(learnings)} learning(s); {suggested} escalation suggestion(s) added.")
    if created:
        print("Run `python3 tools/coach.py list` to review them.")
    return 0


def cmd_list(store: Store, args) -> int:
    rows = store.db.execute(
        "SELECT * FROM coach_proposals WHERE status='pending' ORDER BY created_at ASC"
    ).fetchall()
    if not rows:
        print("No pending proposals.")
        return 0
    print(f"{len(rows)} proposal(s) waiting:\n")
    for r in rows:
        print(f"  {r['id']}  {r['pattern']:<24} cluster={r['cluster_size']:<3} "
             f"{r['suggested_fix'][:70]}")
    print("\nRun `python3 tools/coach.py show <id>` for the full example.")
    return 0


def cmd_show(store: Store, args) -> int:
    row = store.db.execute("SELECT * FROM coach_proposals WHERE id=?", (args.id,)).fetchone()
    if row is None:
        print(f"error: no proposal {args.id}", file=sys.stderr)
        return 1
    d = dict(row)
    for k, v in d.items():
        print(f"{k}: {v}")
    return 0


def _decide(store: Store, proposal_id: str, status: str, note: str) -> int:
    row = store.db.execute("SELECT * FROM coach_proposals WHERE id=?",
                           (proposal_id,)).fetchone()
    if row is None:
        print(f"error: no proposal {proposal_id}", file=sys.stderr)
        return 1
    store.db.execute(
        "UPDATE coach_proposals SET status=?, decided_at=? WHERE id=?",
        (status, utcnow(), proposal_id))
    store.record_event(None, "human", f"coach_proposal_{status}",
                       {"proposal_id": proposal_id, "note": note})
    print(f"{status} {proposal_id}")
    return 0


def cmd_accept(store: Store, args) -> int:
    return _decide(store, args.id, "accepted", args.note or "")


def cmd_reject(store: Store, args) -> int:
    return _decide(store, args.id, "rejected", args.note or "")


def cmd_apply(store: Store, args) -> int:
    rows = store.db.execute(
        "SELECT * FROM coach_proposals WHERE status='accepted' AND "
        "(applied_at IS NULL OR applied_at='') ORDER BY created_at ASC").fetchall()
    if not rows:
        print("Nothing accepted is waiting to be applied.")
        return 0
    is_new = not RULES_FILE.exists()
    with RULES_FILE.open("a", encoding="utf-8") as fh:
        if is_new:
            fh.write("# Rules learned from human corrections\n\n"
                    "<!-- Written by `tools/coach.py apply`. One line per accepted "
                    "proposal, oldest first. Edit or delete lines by hand any time - "
                    "this is a plain file, not a database. See knowledge/README.md. -->\n\n")
        for row in rows:
            fh.write(f"- ({row['intent'] or 'general'}) {row['suggested_fix']}\n")
    ids = [r["id"] for r in rows]
    store.db.executemany(
        "UPDATE coach_proposals SET status='applied', applied_at=? WHERE id=?",
        [(utcnow(), i) for i in ids])
    print(f"Applied {len(rows)} proposal(s) to {RULES_FILE}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="cluster learnings + suggest fixes")
    p_analyze.add_argument("--provider", default=None)

    sub.add_parser("list", help="pending proposals")

    p_show = sub.add_parser("show", help="full detail for one proposal")
    p_show.add_argument("id")

    p_accept = sub.add_parser("accept", help="human accepts a proposal")
    p_accept.add_argument("id")
    p_accept.add_argument("--note", default="")

    p_reject = sub.add_parser("reject", help="human rejects a proposal")
    p_reject.add_argument("id")
    p_reject.add_argument("--note", default="")

    sub.add_parser("apply", help="write accepted proposals into knowledge/rules.md")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    store_ext.ensure_schema(store)
    try:
        if args.command == "analyze":
            return cmd_analyze(store, settings, args)
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "show":
            return cmd_show(store, args)
        if args.command == "accept":
            return cmd_accept(store, args)
        if args.command == "reject":
            return cmd_reject(store, args)
        if args.command == "apply":
            return cmd_apply(store, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except LLMPendingInteractive as exc:
        # Not an error - a pause. Must be caught BEFORE the LLMError branch
        # below (it deliberately is not an LLMError subclass, so a plain
        # `except LLMError` would let this propagate as an unhandled
        # exception - see SIMULATION.md finding 11 and core/llm.py).
        print(str(exc))
        return 3
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
