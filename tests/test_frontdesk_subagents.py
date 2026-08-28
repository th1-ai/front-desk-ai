"""One test per sub-agent folded into this repo - see docs/sub-agents.md.

Specialist Booking AIs share Loop A's triage/draft and only differ in the
tool `compute_pending` dispatches to (`preview_experience`); WhatsApp shares
the whole loop and differs only in `channel` - both are covered more fully
in test_frontdesk_engine.py. This file isolates the behaviour unique to each.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters import get_messaging
from core.config import load_settings
from core.store import Store

import store_ext
from booking import compute_pending
from engine import apply_lane_gate, chat_to_dict, process_message


def _settings(**overrides):
    settings = load_settings(provider="mock", mode="shadow")
    if overrides.get("specialist_enabled"):
        settings.agent.setdefault("subagents", {}).setdefault(
            "specialist_booking", {})["enabled"] = True
    return settings


def _seeded_store(tmp_path, settings, name="subagents.db"):
    store = Store(settings, path=tmp_path / name)
    store_ext.ensure_schema(store)
    sessions = json.loads((REPO_ROOT / "fixtures" / "hotel" / "experiences.json")
                         .read_text(encoding="utf-8"))
    store_ext.seed_experience_sessions(store, sessions)
    return store


def test_specialist_booking_stays_off_by_default_and_routes_back():
    settings = _settings()
    triage = {"intent": "experience_inquiry", "lane": "specialist",
             "booking": {"session_slug": "wine-tasting", "party_size": 2}, "missing_info": []}
    apply_lane_gate(triage, settings)
    assert triage["lane"] == "front_desk"
    assert "experience bookings are not enabled" in triage["missing_info"][0]


def test_specialist_booking_when_enabled_books_a_real_session(tmp_path):
    settings = _settings(specialist_enabled=True)
    store = _seeded_store(tmp_path, settings)
    triage = {"intent": "experience_inquiry", "lane": "specialist",
             "booking": {"session_slug": "wine-tasting", "party_size": 2}, "missing_info": []}
    apply_lane_gate(triage, settings)
    assert triage["lane"] == "specialist"
    outcome = compute_pending(settings, store, triage)
    assert outcome.ok is True and outcome.kind == "experience"
    assert outcome.total_eur == 110   # EUR 55 x 2
    store.close()


def test_seed_experience_sessions_still_accepts_the_deprecated_price_eur_key(tmp_path):
    """Regression test for SIMULATION.md Round-2 finding 4: an experience
    catalogue written before the `price_eur` -> `price` rename (this repo's
    own fixtures/hotel/experiences.json now uses `price`) must keep seeding
    correctly - the old key is a deprecated alias, not a breaking change."""
    settings = _settings(specialist_enabled=True)
    store = Store(settings, path=tmp_path / "legacy-price.db")
    store_ext.ensure_schema(store)
    store_ext.seed_experience_sessions(store, [
        {"slug": "legacy-tasting", "title": "Legacy Tasting", "schedule_label": "Fridays",
         "next_date": "2026-09-18", "start_time": "18:00", "price_eur": 40, "capacity": 10,
         "booked": 0, "venue": "The Cellar", "host": "the sommelier"},
    ])
    session = store_ext.get_experience_session(store, "legacy-tasting")
    assert session is not None
    assert session.price_eur == 40.0
    store.close()


def test_whatsapp_messages_share_the_same_engine_and_are_marked_by_channel(tmp_path):
    settings = _settings()
    store = Store(settings, path=tmp_path / "whatsapp.db")
    store_ext.ensure_schema(store)
    chats = get_messaging(settings).fetch_new(limit=50)
    assert len(chats) == 3
    urgent = next(c for c in chats if c.id == "msg-03")
    item, _ = process_message(settings, store, "whatsapp", chat_to_dict(urgent),
                              channel="whatsapp", provider="mock")
    # The Messenger's own "won't do": payment disputes always escalate.
    assert item.review_status == "needs_human"
    assert item.payload["channel"] == "whatsapp"
    store.close()
