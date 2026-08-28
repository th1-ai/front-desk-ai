"""Tests for tools/booking.py - the preview/finalize split (design decision 1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest

from core.config import load_settings
from core.review import WriteBlocked
from core.store import Store

import store_ext
from booking import (compute_pending, finalize_booking, preview_experience, preview_room,
                     preview_spa, preview_table)


def _settings(mode="shadow"):
    return load_settings(provider="mock", mode=mode)


def _store(tmp_path, name="booking.db"):
    settings = _settings()
    store = Store(settings, path=tmp_path / name)
    store_ext.ensure_schema(store)
    return store


def test_preview_room_computes_a_valid_outcome():
    settings = _settings()
    outcome = preview_room(settings, {"room_type": "classic", "checkin": "2026-09-08",
                                      "checkout": "2026-09-10", "pax": 2})
    assert outcome.ok is True
    assert outcome.total_eur == 410
    assert outcome.needs_human is False


def test_preview_room_missing_fields_needs_a_human():
    settings = _settings()
    outcome = preview_room(settings, {"room_type": "classic"})
    assert outcome.ok is False
    assert outcome.needs_human is True


def test_preview_room_flags_a_large_group():
    settings = _settings()
    outcome = preview_room(settings, {"room_type": "classic", "checkin": "2026-09-21",
                                      "checkout": "2026-09-24", "pax": 20})
    assert outcome.ok is True
    assert outcome.needs_human is True


def test_preview_room_accepts_the_display_name_and_books_the_matching_slug():
    """Regression test for SIMULATION.md Round-2 finding 2: `preview_room` must
    resolve a guest-facing display name ("Garden Room") to its own real,
    currently-bookable room's slug (`garden`) - not reject it as an unknown
    room type just because the wording did not match the internal key."""
    settings = _settings()
    settings.agent["rooms"]["room_types"]["garden"] = {
        "name": "Garden Room", "base_rate": 190, "max_occupancy": 2}
    outcome = preview_room(settings, {"room_type": "Garden Room", "checkin": "2026-09-08",
                                      "checkout": "2026-09-10", "pax": 2})
    assert outcome.ok is True
    assert outcome.needs_human is False
    assert outcome.params["room_type"] == "garden"


def test_preview_table_offers_no_human_when_the_day_is_simply_closed():
    settings = _settings()
    outcome = preview_table(settings, {"date": "2026-09-14", "time": "19:30",
                                       "party_size": 4})
    assert outcome.ok is False
    assert outcome.needs_human is False
    assert "closed" in outcome.error.lower()


def test_preview_spa_matches_by_hint():
    settings = _settings()
    outcome = preview_spa(settings, {"treatment": "a massage please", "date": "2026-09-10",
                                     "time": "15:00"})
    assert outcome.ok is True
    assert outcome.total_eur == 95


def test_preview_experience_rejects_an_oversell():
    settings = _settings()
    store = Store(settings, path=":memory:")
    store_ext.ensure_schema(store)
    sessions = json.loads((REPO_ROOT / "fixtures" / "hotel" / "experiences.json")
                         .read_text(encoding="utf-8"))
    store_ext.seed_experience_sessions(store, sessions)
    outcome = preview_experience(settings, store,
                                 {"session_slug": "afterwork-social", "party_size": 1})
    assert outcome.ok is False
    assert "spot" in outcome.error.lower()
    store.close()


def test_preview_room_shows_the_hotel_s_own_currency_not_a_hardcoded_eur():
    settings = _settings()
    settings.hotel.currency = "GBP"
    outcome = preview_room(settings, {"room_type": "classic", "checkin": "2026-09-08",
                                      "checkout": "2026-09-10", "pax": 2})
    assert "GBP" in outcome.detail
    assert "EUR" not in outcome.detail


def test_preview_spa_shows_the_hotel_s_own_currency():
    settings = _settings()
    settings.hotel.currency = "GBP"
    outcome = preview_spa(settings, {"treatment": "a massage please", "date": "2026-09-10",
                                     "time": "15:00"})
    assert "GBP" in outcome.detail
    assert "EUR" not in outcome.detail


def test_finalize_booking_never_confirms_a_request_the_preview_already_rejected(tmp_path):
    """Regression test for SIMULATION.md finding 2, reproduced with the real
    Monday-closed-restaurant fixture (fixtures/inbound/email-04-table-monday-closed.json):
    a table request for a day the restaurant is closed gets `ok: False` at
    triage time, the guest is correctly told no table was booked, and that
    must never turn into a "confirmed" `table_bookings` row - even once the
    (accurate, declining) reply is approved and sent, and even in mode: live
    where the write guard itself would allow the write."""
    from datetime import date

    from pricing import is_restaurant_closed

    settings = _settings(mode="live")
    restaurant = settings.agent_get("restaurant", {})
    closed_day = date.fromisoformat("2026-09-14")
    assert is_restaurant_closed(closed_day, restaurant.get("closed_weekdays", []))

    outcome = preview_table(settings, {"date": "2026-09-14", "time": "19:30", "party_size": 4})
    assert outcome.ok is False   # the fixture's own trigger for the bug

    store = _store(tmp_path, name="booking-finding2.db")
    item = store.upsert_item("email", "finding2-1", kind="message", payload={"channel": "email"})
    item = store.set_fields(item.id, draft={"pending_booking": outcome.as_dict()})
    item = store.transition(item.id, "pending_review")
    item = store.transition(item.id, "approved")
    item = store.transition(item.id, "sending")   # what claim_for_send() does at send time

    result = finalize_booking(settings, store, item)
    assert result.ok is False
    assert result.kind == "table"

    rows = store.db.execute("SELECT * FROM table_bookings WHERE item_id=?",
                            (item.id,)).fetchall()
    assert len(rows) == 0, "a rejected preview must never become a confirmed booking row"
    store.close()


def test_compute_pending_dispatches_on_intent():
    settings = _settings()
    store = Store(settings, path=":memory:")
    store_ext.ensure_schema(store)
    outcome = compute_pending(settings, store, {"intent": "thank_you"})
    assert outcome.kind == "none" and outcome.ok is True
    store.close()


def test_finalize_booking_refuses_to_write_in_shadow_mode_even_directly(tmp_path):
    settings = _settings(mode="shadow")
    store = _store(tmp_path)
    item = store.upsert_item("email", "direct-1", kind="message", payload={"channel": "email"})
    item = store.set_fields(item.id, draft={"pending_booking": {
        "kind": "room", "ok": True, "total_eur": 200,
        "params": {"room_type": "classic", "checkin": "2026-09-08", "checkout": "2026-09-09",
                  "pax": 2}}})
    # never approved - finalize must refuse, exactly like any other guarded write.
    with pytest.raises(WriteBlocked):
        finalize_booking(settings, store, item)
    store.close()
