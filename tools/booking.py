"""tools/booking.py - the seven actions Front Desk AI and the Specialist share.

Two phases, on purpose (see docs/how-it-works.md, design decision 1):

**Preview (`compute_pending`)** runs during triage, for every message,
regardless of mode. It validates the guest's request against the property's
own data - room types, the spa menu, the restaurant's closed days, live
experience capacity - and returns what *would* happen. It never writes
anything. This is what lets the draft prompt say "booked, ref GM-1234" only
when the code has already confirmed the booking is valid.

**Finalize (`finalize_booking`)** runs once, at send time, only for an item a
human approved or edited (`tools/review.py send`). It is the only function in
this module that touches `store_ext`'s tables or calls an adapter write. It
re-checks `core.review.assert_write_allowed` itself, so even a future caller
that skips the review queue cannot make it write in shadow mode.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.adapters import get_messaging, get_pms
from core.adapters.base import AdapterError
from core.review import WriteBlocked, assert_write_allowed
from core.store import Item, Store, utcnow

import store_ext
from pricing import (UnknownRoomType, UnknownTreatment, is_large_group, is_large_party,
                     is_restaurant_closed, match_room_type, match_treatment, nights_between,
                     stay_total)


@dataclass
class BookingOutcome:
    """What a booking action decided, whether or not it has been written yet."""

    ok: bool
    kind: str                       # room | table | spa | experience | note | forward | none
    ref: str = ""
    detail: str = ""
    error: str = ""
    needs_human: bool = False
    total_eur: float = 0.0
    params: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "kind": self.kind, "ref": self.ref, "detail": self.detail,
               "error": self.error, "needs_human": self.needs_human,
               "total_eur": self.total_eur, "params": self.params}


def _ref(prefix: str) -> str:
    return f"{prefix}-{random.randint(1000, 9999)}"


# --------------------------------------------------------------------------
# preview - read-only, runs for every message regardless of mode
# --------------------------------------------------------------------------
def preview_room(settings, booking: dict) -> BookingOutcome:
    room_types = settings.agent_get("rooms.room_types", {})
    room_type = booking.get("room_type")
    checkin, checkout = booking.get("checkin"), booking.get("checkout")
    pax = int(booking.get("pax") or 2)
    if not (room_type and checkin and checkout):
        return BookingOutcome(False, "room", error="missing room type or dates",
                              needs_human=True, params=booking)
    try:
        # The guest, or the model triaging them, rarely says the internal
        # slug - match_room_type accepts the property's own display name or
        # a near-match (same rule spa treatments already get) and resolves
        # it to the slug pricing.py's dict lookups need.
        room_type = match_room_type(room_type, room_types)
        total = stay_total(room_type, checkin, checkout, room_types,
                           season=settings.agent_get("rooms.season_multiplier"),
                           weekend_multiplier=float(settings.agent_get(
                               "rooms.weekend_multiplier", 1.08)),
                           weekend_days=tuple(settings.agent_get("rooms.weekend_days", [4, 5])))
        nights = nights_between(checkin, checkout)
    except UnknownRoomType as exc:
        return BookingOutcome(False, "room", error=str(exc), needs_human=True, params=booking)
    except ValueError as exc:
        return BookingOutcome(False, "room", error=str(exc), needs_human=True, params=booking)
    large = is_large_group(pax=pax,
                           large_group_pax=int(settings.agent_get("rooms.large_group_pax", 15)))
    name = room_types[room_type]["name"]
    currency = settings.hotel.currency
    detail = (f"{name} - {checkin} to {checkout} ({nights} night(s)) - {pax} guest(s) - "
             f"{currency} {total:.0f}")
    return BookingOutcome(True, "room", detail=detail, needs_human=large, total_eur=total,
                          params={**booking, "room_type": room_type, "pax": pax,
                                 "nights": nights})


def preview_table(settings, booking: dict) -> BookingOutcome:
    restaurant = settings.agent_get("restaurant", {})
    booking_date, time_ = booking.get("date"), booking.get("time") or restaurant.get(
        "default_time", "19:30")
    party_size = int(booking.get("party_size") or 2)
    if not booking_date:
        return BookingOutcome(False, "table", error="missing date", needs_human=True,
                              params=booking)
    try:
        day = date.fromisoformat(str(booking_date)[:10])
    except ValueError:
        return BookingOutcome(False, "table", error=f"'{booking_date}' is not a valid date",
                              needs_human=True, params=booking)
    name = restaurant.get("name", "the restaurant")
    if is_restaurant_closed(day, restaurant.get("closed_weekdays", [])):
        return BookingOutcome(False, "table",
                              error=f"{name} is closed on {day.strftime('%A')}s - offer "
                                    "the nearest other evening instead.",
                              params=booking)
    large = is_large_party(party_size,
                           int(settings.agent_get("restaurant.large_party_size", 10)))
    detail = f"{name} - {booking_date} {time_} - {party_size} guest(s)"
    return BookingOutcome(True, "table", detail=detail, needs_human=large,
                          params={**booking, "time": time_, "party_size": party_size})


def preview_spa(settings, booking: dict) -> BookingOutcome:
    spa = settings.agent_get("spa", {})
    menu = spa.get("menu", [])
    treatment = booking.get("treatment")
    booking_date, time_ = booking.get("date"), booking.get("time")
    if not (treatment and booking_date and time_):
        return BookingOutcome(False, "spa", error="missing treatment, date or time",
                              needs_human=True, params=booking)
    try:
        match = match_treatment(treatment, menu, booking.get("party_size"))
    except UnknownTreatment as exc:
        return BookingOutcome(False, "spa", error=str(exc), needs_human=True, params=booking)
    currency = settings.hotel.currency
    detail = (f"{spa.get('name', 'the spa')} - {match.title} - {booking_date} {time_} - "
             f"{currency} {match.total_eur:.0f}")
    params = {**booking, "treatment_id": match.treatment_id, "treatment": match.title,
             "party_size": match.party_size, "time": time_}
    return BookingOutcome(True, "spa", detail=detail, total_eur=match.total_eur, params=params)


def preview_experience(settings, store: Store, booking: dict) -> BookingOutcome:
    slug = booking.get("session_slug")
    party_size = int(booking.get("party_size") or 1)
    if not slug:
        return BookingOutcome(False, "experience", error="missing session_slug",
                              needs_human=True, params=booking)
    session = store_ext.get_experience_session(store, slug)
    if session is None:
        return BookingOutcome(False, "experience", error=f"unknown experience session: {slug}",
                              needs_human=True, params=booking)
    if party_size > session.spots_left:
        return BookingOutcome(
            False, "experience",
            error=f"only {session.spots_left} spot(s) left for {session.title} - offer to "
                  "book those or suggest the next session.",
            params=booking)
    total = session.price_eur * party_size
    currency = settings.hotel.currency
    detail = (f"{session.title} - {session.next_date} {session.start_time} - "
             f"{party_size} guest(s) - {currency} {total:.0f}")
    params = {**booking, "party_size": party_size, "session_title": session.title,
             "date": session.next_date}
    return BookingOutcome(True, "experience", detail=detail, total_eur=total, params=params)


def compute_pending(settings, store: Store, triage: dict) -> BookingOutcome:
    """Dispatch on the triage intent to the matching preview function.

    Returns ``BookingOutcome(kind="none")`` for an intent with nothing to
    book (a plain question, a thank-you) - the draft step still runs, it just
    has no booking result to reference.
    """
    intent = triage.get("intent")
    booking = triage.get("booking") or {}
    if intent == "room_booking":
        return preview_room(settings, booking)
    if intent == "table_booking":
        return preview_table(settings, booking)
    if intent == "spa_booking":
        return preview_spa(settings, booking)
    if intent == "experience_inquiry":
        return preview_experience(settings, store, booking)
    if intent == "modification":
        # Nothing here checks whether an extended stay, a date change or a
        # party-size change is actually available - unlike a fresh booking,
        # tools/pricing.py has no "modify" formula to fall back on, so this
        # always needs a person rather than let the model guess at
        # availability. See docs/how-it-works.md design decision 11 and
        # SIMULATION.md finding 14.
        return BookingOutcome(True, "none", needs_human=True,
                              error="modification requested - a person needs to check the "
                                    "current booking and availability before confirming "
                                    "anything; this is not validated automatically.")
    if intent == "guest_fact":
        fact = triage.get("guest_fact") or {}
        return BookingOutcome(True, "note", detail=fact.get("note", ""),
                              params={**fact})
    return BookingOutcome(True, "none")


# --------------------------------------------------------------------------
# finalize - the only place that writes. Runs once, at send time.
# --------------------------------------------------------------------------
def finalize_booking(settings, store: Store, item: Item) -> BookingOutcome:
    """Actually create the booking (and any PMS note / staff forward) for an
    item a human has approved or edited. Called once from
    ``tools/review.py send``, on an item already flipped to ``sending`` by
    :meth:`core.store.Store.claim_for_send`.

    Re-checks the write guard itself (``pms_write``) so this function can
    never write in shadow mode or on ``--dry-run``, no matter who calls it.
    """
    pending = (item.draft or {}).get("pending_booking") or {"kind": "none"}
    kind = pending.get("kind", "none")
    if kind == "none":
        return BookingOutcome(True, "none")

    if not pending.get("ok", False):
        # The preview computed at triage time already rejected this request
        # (closed day, no availability, unknown room type, an oversell) - the
        # guest-facing reply that was approved and sent already says so.
        # Writing a "confirmed" row here anyway would contradict that reply
        # with a phantom booking nobody would think to look for. Checked
        # before the write guard on purpose: a rejected preview must never
        # become a confirmed row, in shadow OR live, approved or not. See
        # docs/how-it-works.md design decision 1 and SIMULATION.md finding 2.
        outcome = BookingOutcome(False, kind, error=pending.get("error") or
                                 "the booking preview did not succeed at triage time; "
                                 "nothing was written", params=pending.get("params") or {})
        store.record_event(item.id, "agent", "booking_not_finalized",
                           {"kind": kind, "reason": outcome.error})
        return outcome

    assert_write_allowed(settings, "pms_write", item)
    channel = (item.payload or {}).get("channel", "")
    guest_name = (item.payload or {}).get("from_name") or (item.payload or {}).get(
        "from_number") or "Guest"
    params = pending.get("params") or {}
    now = utcnow()

    if kind == "room":
        ref = _ref("RES")
        store.db.execute(
            "INSERT INTO room_bookings (id, ref, item_id, guest_name, room_type, checkin, "
            "checkout, pax, total_eur, channel, status, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), ref, item.id, guest_name, params.get("room_type"),
             params.get("checkin"), params.get("checkout"), int(params.get("pax", 2)),
             float(pending.get("total_eur", 0.0)), channel, "confirmed",
             "Booked by Front Desk AI via guest messaging", now))
        outcome = BookingOutcome(True, "room", ref=ref, detail=pending.get("detail", ""),
                                 total_eur=pending.get("total_eur", 0.0), params=params)
    elif kind == "table":
        ref = _ref("TBL")
        store.db.execute(
            "INSERT INTO table_bookings (id, ref, item_id, guest_name, party_size, date, "
            "time, dietary_notes, special_requests, channel, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), ref, item.id, guest_name, int(params.get("party_size", 2)),
             params.get("date"), params.get("time"), params.get("dietary_notes"),
             params.get("special_requests"), channel, "confirmed", now))
        outcome = BookingOutcome(True, "table", ref=ref, detail=pending.get("detail", ""),
                                 params=params)
    elif kind == "spa":
        ref = _ref("SPA")
        store.db.execute(
            "INSERT INTO spa_bookings (id, ref, item_id, guest_name, treatment_id, treatment, "
            "date, time, party_size, price_eur, reservation_ref, channel, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), ref, item.id, guest_name, params.get("treatment_id"),
             params.get("treatment"), params.get("date"), params.get("time"),
             int(params.get("party_size", 1)), float(pending.get("total_eur", 0.0)),
             params.get("reservation_ref"), channel, "confirmed", now))
        # the mandatory PMS note - see docs/how-it-works.md design decision 4.
        if params.get("reservation_ref"):
            _append_pms_note(settings, params["reservation_ref"],
                             f"Spa: {params.get('treatment')} {params.get('date')} "
                             f"{params.get('time')} ({ref})", item)
        outcome = BookingOutcome(True, "spa", ref=ref, detail=pending.get("detail", ""),
                                 total_eur=pending.get("total_eur", 0.0), params=params)
    elif kind == "experience":
        ref = _ref("EXP")
        slug = params.get("session_slug")
        store.db.execute(
            "INSERT INTO experience_bookings (id, ref, item_id, session_slug, guest_name, "
            "party_size, date, total_eur, occasion, channel, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), ref, item.id, slug, guest_name,
             int(params.get("party_size", 1)), params.get("date"),
             float(pending.get("total_eur", 0.0)), params.get("occasion"), channel,
             "confirmed", now))
        store.db.execute("UPDATE experience_sessions SET booked = booked + ? WHERE slug=?",
                         (int(params.get("party_size", 1)), slug))
        outcome = BookingOutcome(True, "experience", ref=ref, detail=pending.get("detail", ""),
                                 total_eur=pending.get("total_eur", 0.0), params=params)
    elif kind == "note":
        note_ref = params.get("reservation_ref")
        note_text = params.get("note", "")
        if note_ref:
            _append_pms_note(settings, note_ref, note_text, item)
        forward_result = forward_to_team(settings, params.get("department", "guest_relations"),
                                         note_text, item)
        outcome = BookingOutcome(True, "note", detail=note_text, ref=note_ref or "",
                                 params={**params, "forwarded": forward_result})
    else:
        outcome = BookingOutcome(True, "none")

    store.record_event(item.id, "agent", "booking_finalized",
                       {"kind": outcome.kind, "ref": outcome.ref})
    return outcome


def _append_pms_note(settings, reservation_ref: str, text: str, item: Item) -> None:
    """Best-effort PMS note. A read-only or unreachable PMS must never break a send.

    ``item`` is passed through to the write guard so an already-approved item
    can pass even in shadow mode (see ``core.review.APPROVED_STATES``).
    """
    try:
        pms = get_pms(settings)
        pms.add_note(reservation_ref, text, item=item)
    except (AdapterError, WriteBlocked, NotImplementedError):
        pass


def forward_to_team(settings, department: str, summary: str, item: Item | None = None) -> dict:
    """Notify staff through the messaging adapter's ``notify_staff`` - a real
    write, unlike the source system's simulated log line (design decision 5).
    ``item`` is passed through to the write guard - see ``_append_pms_note``.
    """
    label = {"fnb": "F&B team", "housekeeping": "Housekeeping", "maintenance": "Maintenance",
            "guest_relations": "Guest Relations"}.get(department, department)
    try:
        messaging = get_messaging(settings)
        result = messaging.notify_staff(f"[{label}] {summary}", item=item)
        return {"ok": True, "team": label, **result}
    except (AdapterError, WriteBlocked, NotImplementedError) as exc:
        return {"ok": False, "team": label, "error": str(exc)}
