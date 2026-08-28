"""Tests for Loop B (tools/confirmations.py) - pure planning functions first,
then the impure runner against the bundled PMS fixtures.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.config import load_settings
from core.store import Store

import store_ext
from confirmations import (LandedBooking, guest_language, plan_confirmation, plan_reminders,
                           process_new_bookings)


def _settings():
    return load_settings(provider="mock", mode="shadow")


def test_plan_confirmation_is_pending_review_under_the_threshold():
    settings = _settings()
    booking = LandedBooking(ref="RES-1", kind="room", origin="pms", total_eur=400,
                            event_date="2026-09-14", room_type_name="Classic Room")
    plan = plan_confirmation(booking, settings, "en")
    assert plan["status"] == "pending_review"
    assert "RES-1" in plan["body"]


def test_plan_confirmation_needs_a_human_over_the_threshold():
    settings = _settings()
    booking = LandedBooking(ref="RES-2", kind="room", origin="pms", total_eur=1800,
                            event_date="2026-10-02", room_type_name="Aurora Suite")
    plan = plan_confirmation(booking, settings, "en")
    assert plan["status"] == "needs_human"


def test_plan_confirmation_is_none_for_a_front_desk_origin_booking():
    settings = _settings()
    booking = LandedBooking(ref="TBL-1", kind="table", origin="front_desk",
                            needs_confirmation=False, event_date="2026-09-14")
    assert plan_confirmation(booking, settings, "en") is None


def test_plan_reminders_drops_a_lapsed_slot():
    settings = _settings()
    booking = LandedBooking(ref="RES-3", kind="room", origin="pms", event_date="2026-08-01")
    reminders = plan_reminders(booking, settings, "en", now=datetime(2026, 8, 25, 9, 0))
    assert reminders == []   # the room_reminder ("t-1d-17:00") is long past


def test_plan_reminders_queues_a_future_room_reminder():
    settings = _settings()
    booking = LandedBooking(ref="RES-4", kind="room", origin="pms", event_date="2026-09-14")
    reminders = plan_reminders(booking, settings, "en", now=datetime(2026, 8, 25, 9, 0))
    assert len(reminders) == 1
    assert reminders[0]["due_at"].startswith("2026-09-13T17:00")


def test_plan_reminders_skips_table_bookings_by_default():
    settings = _settings()
    assert settings.agent_get("confirmations.restaurant_reminder_enabled") is False
    booking = LandedBooking(ref="TBL-2", kind="table", origin="front_desk", event_date="2026-09-14",
                            event_time="19:30")
    assert plan_reminders(booking, settings, "en", now=datetime(2026, 8, 25)) == []


def test_guest_language_prefers_correspondence_then_phone_then_default():
    settings = _settings()
    spanish_phone = LandedBooking(ref="R", kind="room", origin="pms", guest_phone="+34 600 000 001")
    assert guest_language(spanish_phone, settings) == "es"

    with_correspondence = LandedBooking(ref="R", kind="room", origin="front_desk",
                                        guest_phone="+34 600 000 001",
                                        correspondence_language="en")
    assert guest_language(with_correspondence, settings) == "en"

    no_signal = LandedBooking(ref="R", kind="room", origin="pms")
    assert guest_language(no_signal, settings) == settings.hotel.default_language


def test_process_new_bookings_is_idempotent(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "confirmations.db")
    store_ext.ensure_schema(store)
    now = datetime(2026, 8, 25, 9, 0)

    first = process_new_bookings(settings, store, now=now)
    assert first["bookings"] == 3
    assert first["confirmations_queued"] == 3
    assert first["reminders_queued"] == 2
    assert first["lapsed"] == 1

    second = process_new_bookings(settings, store, now=now)
    assert second == {"bookings": 0, "confirmations_queued": 0, "reminders_queued": 0,
                      "lapsed": 0}
    store.close()
