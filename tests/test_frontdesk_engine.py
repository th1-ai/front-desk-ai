"""Tests for Loop A (tools/engine.py) against the bundled fixtures, with
provider=mock. No network, no credentials - this is what `make demo` runs on.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import json

from core.adapters import get_email, get_messaging
from core.config import load_settings
from core.llm import LLMPendingInteractive
from core.store import Store

import store_ext
from engine import chat_to_dict, email_to_dict, needs_human_for, process_message

EXPECTED_STATUS = {
    "email-01": "pending_review", "email-02": "pending_review", "email-03": "needs_human",
    "email-04": "pending_review", "email-05": "pending_review", "email-06": "pending_review",
    "email-07": "needs_human", "email-08": "needs_human", "email-09": "needs_human",
    "email-10": "pending_review",
}


def _settings():
    return load_settings(provider="mock", mode="shadow")


def _store(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "engine.db")
    store_ext.ensure_schema(store)
    return settings, store


def _emails():
    return get_email(_settings()).fetch_unread(limit=50)


def test_ten_email_fixtures_are_present():
    messages = _emails()
    assert len(messages) == 10
    assert set(m.id for m in messages) == set(EXPECTED_STATUS)


def test_every_email_matches_its_expected_status(tmp_path):
    settings, store = _store(tmp_path)
    for msg in _emails():
        item, did_work = process_message(settings, store, "email", email_to_dict(msg),
                                         channel="email", provider="mock")
        assert did_work is True
        assert item.review_status == EXPECTED_STATUS[msg.id], msg.id
        assert item.draft is not None and item.draft.get("body")
    store.close()


def test_large_group_room_request_escalates_even_though_the_booking_is_valid(tmp_path):
    settings, store = _store(tmp_path)
    msg = next(m for m in _emails() if m.id == "email-03")
    item, _ = process_message(settings, store, "email", email_to_dict(msg), channel="email",
                              provider="mock")
    assert item.review_status == "needs_human"
    pending = (item.draft or {}).get("pending_booking") or {}
    assert pending.get("ok") is True   # the booking itself is valid - the size is the issue
    store.close()


def test_experience_inquiry_is_gated_back_to_front_desk_when_specialist_is_off(tmp_path):
    settings, store = _store(tmp_path)
    assert settings.agent_get("subagents.specialist_booking.enabled") is False
    msg = next(m for m in _emails() if m.id == "email-08")
    item, _ = process_message(settings, store, "email", email_to_dict(msg), channel="email",
                              provider="mock")
    assert (item.draft or {}).get("lane") == "front_desk"
    assert item.review_status == "needs_human"
    store.close()


def test_whatsapp_channel_is_recorded_on_the_item(tmp_path):
    settings, store = _store(tmp_path)
    chats = get_messaging(settings).fetch_new(limit=50)
    msg = next(c for c in chats if c.id == "msg-01")
    item, _ = process_message(settings, store, "whatsapp", chat_to_dict(msg),
                              channel="whatsapp", provider="mock")
    assert item.payload.get("channel") == "whatsapp"
    assert (item.draft or {}).get("subject") == ""
    store.close()


def test_shadow_mode_never_sends_anything(tmp_path):
    settings, store = _store(tmp_path)
    for msg in _emails():
        process_message(settings, store, "email", email_to_dict(msg), channel="email",
                        provider="mock")
    counts = store.counts()
    assert counts.get("sent", 0) == 0
    assert counts.get("auto_sent", 0) == 0
    store.close()


def test_rerun_is_idempotent_and_does_not_reprocess(tmp_path):
    settings, store = _store(tmp_path)
    messages = _emails()
    for msg in messages:
        process_message(settings, store, "email", email_to_dict(msg), channel="email",
                        provider="mock")
    for msg in messages:
        item, did_work = process_message(settings, store, "email", email_to_dict(msg),
                                         channel="email", provider="mock")
        assert did_work is False
    assert len(store.list_items(limit=100)) == 10
    store.close()


def test_needs_human_for_respects_the_confidence_threshold():
    settings = _settings()
    threshold = float(settings.agent_get("confidence_threshold", 0.80))
    from booking import BookingOutcome
    ok_pending = BookingOutcome(True, "none")
    below = {"intent": "question", "confidence": threshold - 0.01, "missing_info": []}
    above = {"intent": "question", "confidence": threshold + 0.1, "missing_info": []}
    assert needs_human_for(below, ok_pending, {}, settings) is True
    assert needs_human_for(above, ok_pending, {}, settings) is False


def test_interactive_provider_resumes_at_draft_without_re_asking_triage(tmp_path):
    """Regression test for a real bug found during onboarding simulation:
    the interactive provider needs two separate answers per item (triage,
    then draft) - a second run must not lose the first answer, and must not
    ask triage again while waiting on draft. See the docstring on
    tools/engine.py:process_message.
    """
    settings = load_settings(provider="interactive", mode="shadow")
    store = Store(settings, path=tmp_path / "interactive.db")
    store_ext.ensure_schema(store)
    msg = {"id": "test-interactive-1", "from": "guest@example.com", "from_name": "Guest",
          "subject": "Check-in time?", "body": "What time is check-in?",
          "received_at": "2026-08-20T08:00:00+00:00", "reservation_ref": None}

    pending_dir = settings.root / "data" / "pending"
    triage_answer = pending_dir / "triage-test-interactive-1.answer.json"
    draft_answer = pending_dir / "draft-test-interactive-1.answer.json"
    for leftover in pending_dir.glob("*test-interactive-1*"):
        leftover.unlink()
    try:
        # Round 1: no answers yet - triage pends.
        try:
            process_message(settings, store, "email", msg, channel="email")
            assert False, "expected LLMPendingInteractive"
        except LLMPendingInteractive as exc:
            assert exc.pending_id == "triage-test-interactive-1"

        # Round 2: triage answered - draft should pend next, not triage again.
        triage_answer.write_text(json.dumps({
            "intent": "question", "lane": "front_desk", "language": "en", "confidence": 0.95,
            "booking": None, "guest_fact": None, "missing_info": [], "escalation": None,
            "reason": "check-in time question"}), encoding="utf-8")
        try:
            process_message(settings, store, "email", msg, channel="email")
            assert False, "expected LLMPendingInteractive"
        except LLMPendingInteractive as exc:
            assert exc.pending_id == "draft-test-interactive-1"
        item = store.get_by_external("email", "test-interactive-1")
        assert item.intent == "question"        # triage's answer was kept
        assert "_triage_cache" in item.payload   # ... and cached for this round

        # Round 3: draft answered too - the item should now be fully queued.
        draft_answer.write_text(json.dumps({
            "subject": "Re: Check-in time?", "body": "Check-in is from 15:00.",
            "needs_human": False, "ai_suggested_reply": None}), encoding="utf-8")
        item, did_work = process_message(settings, store, "email", msg, channel="email")
        assert did_work is True
        assert item.review_status == "pending_review"
        assert item.draft["body"] == "Check-in is from 15:00."

        # Round 4: nothing left to ask - fully idempotent.
        item, did_work = process_message(settings, store, "email", msg, channel="email")
        assert did_work is False
    finally:
        for leftover in pending_dir.glob("*test-interactive-1*"):
            leftover.unlink()
        store.close()
