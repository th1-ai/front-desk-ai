"""Tests for tools/review.py - the queue and the send step.

`send` is the only place a booking is actually written (design decision 1):
this file checks the whole chain, approve -> send -> finalize -> adapter.send.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters import get_email
from core.config import load_settings, sub_data_dir
from core.review import approve
from core.store import Store

import review as review_tool
import store_ext
from engine import email_to_dict, process_message


def _settings(mode="shadow"):
    return load_settings(provider="mock", mode=mode)


def _args(**kw):
    return SimpleNamespace(**kw)


def test_send_with_nothing_approved_does_nothing(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "review1.db")
    store_ext.ensure_schema(store)
    assert review_tool.cmd_send(store, settings, _args(limit=20)) == 0
    assert store.counts().get("sent", 0) == 0
    store.close()


def test_approve_then_send_is_blocked_in_shadow_mode_even_when_approved(tmp_path):
    """Regression test for SIMULATION.md finding 1: `mode: shadow` used to let
    an already-approved item through `send` for real. Now the guard blocks
    EVERY item in shadow, approved or not - the approval is only ever
    recorded (core/review.py:evaluate_write). Nothing is sent, no booking is
    written, and the outbox stays empty."""
    settings = _settings(mode="shadow")
    store = Store(settings, path=tmp_path / "review-shadow.db")
    store_ext.ensure_schema(store)

    msg = next(m for m in get_email(settings).fetch_unread(limit=50) if m.id == "email-02")
    item, _ = process_message(settings, store, "email", email_to_dict(msg), channel="email",
                              provider="mock")
    assert item.review_status == "pending_review"

    approve(store, item.id)
    rc = review_tool.cmd_send(store, settings, _args(limit=20))
    assert rc == 1   # blocked -> counted as failed, never a crash

    blocked_item = store.get_item(item.id)
    assert blocked_item.review_status == "failed"
    assert blocked_item.sent_message_id is None

    rows = store.db.execute("SELECT * FROM room_bookings WHERE item_id=?",
                            (item.id,)).fetchall()
    assert len(rows) == 0, "shadow must never write a booking either, approved or not"
    store.close()


def test_approve_then_send_finalizes_the_booking_and_sends_the_email_in_live_mode(tmp_path):
    """The same approve -> send -> finalize chain as above, but in
    `mode: live` with the approval satisfied - this is the path that must
    actually work once a hotel goes live (workflows/90-go-live.md)."""
    settings = _settings(mode="live")
    store = Store(settings, path=tmp_path / "review2.db")
    store_ext.ensure_schema(store)

    msg = next(m for m in get_email(settings).fetch_unread(limit=50) if m.id == "email-02")
    item, _ = process_message(settings, store, "email", email_to_dict(msg), channel="email",
                              provider="mock")
    assert item.review_status == "pending_review"

    approve(store, item.id)
    rc = review_tool.cmd_send(store, settings, _args(limit=20))
    assert rc == 0

    sent_item = store.get_item(item.id)
    assert sent_item.review_status == "sent"
    assert sent_item.sent_message_id

    rows = store.db.execute("SELECT * FROM room_bookings WHERE item_id=?",
                            (item.id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["room_type"] == "classic"
    assert rows[0]["total_eur"] == 410

    outbox = sub_data_dir("exports") / "sent_email.jsonl"
    assert outbox.exists()
    assert "priya.shah@example.com" in outbox.read_text(encoding="utf-8")
    store.close()


def test_send_without_approval_is_blocked_even_in_live_mode(tmp_path):
    """`mode: live` does not mean unapproved items start sending - only
    `require_approval_for` actions still gate on `approved`/`edited`."""
    settings = _settings(mode="live")
    store = Store(settings, path=tmp_path / "review-live-unapproved.db")
    store_ext.ensure_schema(store)
    msg = next(m for m in get_email(settings).fetch_unread(limit=50) if m.id == "email-01")
    item, _ = process_message(settings, store, "email", email_to_dict(msg), channel="email",
                              provider="mock")
    assert item.review_status == "pending_review"   # never approved
    # send only ever acts on approved/edited items (claim_for_send), so an
    # unapproved item is simply not picked up - nothing to send.
    rc = review_tool.cmd_send(store, settings, _args(limit=20))
    assert rc == 0
    assert store.get_item(item.id).review_status == "pending_review"
    store.close()


def test_reject_is_terminal_and_records_a_learning(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "review3.db")
    store_ext.ensure_schema(store)
    msg = next(m for m in get_email(settings).fetch_unread(limit=50) if m.id == "email-01")
    item, _ = process_message(settings, store, "email", email_to_dict(msg), channel="email",
                              provider="mock")
    review_tool.cmd_reject(store, _args(id=item.id, reason="wrong tone"))
    rejected = store.get_item(item.id)
    assert rejected.review_status == "rejected"
    learnings = store.list_learnings()
    assert any(l["source_item"] == item.id for l in learnings)
    store.close()


def test_shipped_signature_example_reaches_a_guest_with_no_frontmatter(tmp_path):
    """Regression test for SIMULATION.md Round-2 finding 1: knowledge/signature.example.md
    ships with a `---\\nsubject: ""\\n---` YAML frontmatter block, and workflows/00-setup.md
    step 3 tells every hotel to `cp knowledge/signature.example.md knowledge/signature.md`
    verbatim. `Email.with_signature()` (called by every email adapter's `send()`) must strip
    that frontmatter before it reaches a guest - using this repo's own shipped example file,
    not a synthetic fixture, so this fails the moment anyone reintroduces the raw append."""
    from core.adapters.base import AdapterConfig
    from core.adapters.email_mock import MockEmail

    example = REPO_ROOT / "knowledge" / "signature.example.md"
    sig_path = tmp_path / "signature.md"
    sig_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    settings = _settings(mode="live")
    email = MockEmail(settings, AdapterConfig(
        adapter="mock", options={"signature_file": str(sig_path)}))
    body = email.with_signature("Hi Sam, breakfast is served 7-10am daily.")

    assert "---" not in body
    assert "subject:" not in body
    assert "team" in body.lower()  # the sign-off line itself still made it through


def test_sample_item_shows_marker_in_list_line_and_show(tmp_path, capsys):
    """core/store.py tags an item read through a mock adapter outside `make
    demo` as `_sample` (`Item.is_sample`) - a human working the real queue
    must see that at a glance, in both `list` and `show`."""
    settings = _settings()
    store = Store(settings, path=tmp_path / "review-sample.db")
    store_ext.ensure_schema(store)

    item = store.upsert_item("email", "sample-marker-1", kind="message",
                             payload={"subject": "Test guest email",
                                      "from": "guest@example.com", "_sample": True})
    assert item.is_sample

    capsys.readouterr()
    review_tool._print_item_line(item)
    assert "[SAMPLE DATA]" in capsys.readouterr().out

    rc = review_tool.cmd_show(store, _args(id=item.id))
    assert rc == 0
    assert "[SAMPLE DATA]" in capsys.readouterr().out
    store.close()
