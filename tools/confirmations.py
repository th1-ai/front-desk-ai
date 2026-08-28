"""tools/confirmations.py - Loop B, the Greeter: confirmations and reminders.

A second, independent loop from Loop A (tools/engine.py). Loop A answers
guests who wrote in. Loop B watches for a booking that *landed* with nobody
having written anything yet - a room booked straight through the PMS, an OTA,
the booking widget, or an accepted upsell - and makes sure the guest still
gets a personalised confirmation and the timed reminders that protect show-up
rates, in their own language, the instant it happens. See
docs/how-it-works.md ("Loop B - the Greeter") for the full picture.

Two landed-booking origins, merged by :func:`collect_landed_bookings`:

``pms``          a reservation in the PMS/OTA that nobody in this repo has
                 confirmed yet - needs both a confirmation and a reminder.
``front_desk``   a room/table/spa/experience booking Loop A already created
                 (``tools/booking.py:finalize_booking``, only ever called
                 after a human approved the guest-facing reply that IS the
                 confirmation) - needs only the reminder.

Pure functions, no model call at all: :func:`plan_confirmation` and
:func:`plan_reminders` take plain values and return plain data. Everything
that touches the store or an adapter lives in :func:`collect_landed_bookings`
and :func:`process_new_bookings`, kept separate on purpose so the planning
logic is trivial to unit test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from core.config import Settings
from core.i18n import detect_language
from core.store import Store, utcnow
from core.templates import render_string

import store_ext

#: subject/body templates, one per (event, booking kind, language). Falls back
#: to "en" for any language not listed here - see docs/how-it-works.md design
#: decision 7. Add a language by adding one more dict entry; nothing else in
#: this module needs to change.
TEMPLATES: dict[str, dict[str, dict[str, dict[str, str]]]] = {
    "confirmation": {
        "room": {
            "en": {"subject": "Your stay at {{hotel_name}}, {{event_date}}",
                  "body": "Hello {{guest_name}},\n\nYou are all set: {{room_type_name}} "
                          "from {{event_date}}, reference {{ref}}.\n\nWe look forward to "
                          "welcoming you."},
            "fr": {"subject": "Votre sejour a {{hotel_name}}, {{event_date}}",
                  "body": "Bonjour {{guest_name}},\n\nC'est confirme : {{room_type_name}} "
                          "a partir du {{event_date}}, reference {{ref}}.\n\nAu plaisir de "
                          "vous accueillir."},
            "es": {"subject": "Su estancia en {{hotel_name}}, {{event_date}}",
                  "body": "Hola {{guest_name}},\n\nTodo confirmado: {{room_type_name}} "
                          "desde el {{event_date}}, referencia {{ref}}.\n\nEsperamos "
                          "darle la bienvenida."},
            "pt": {"subject": "A sua estadia no {{hotel_name}}, {{event_date}}",
                  "body": "Ola {{guest_name}},\n\nEsta confirmado: {{room_type_name}} "
                          "a partir de {{event_date}}, referencia {{ref}}.\n\nEsperamos "
                          "receber voce em breve."},
        },
    },
    "reminder": {
        "room": {
            "en": {"subject": "See you tomorrow at {{hotel_name}}",
                  "body": "Hello {{guest_name}},\n\nJust a note that we look forward to "
                          "welcoming you tomorrow, {{event_date}}. Reference {{ref}}."},
            "fr": {"subject": "A demain a {{hotel_name}}",
                  "body": "Bonjour {{guest_name}},\n\nUn petit mot pour vous dire que nous "
                          "avons hate de vous accueillir demain, {{event_date}}. "
                          "Reference {{ref}}."},
            "es": {"subject": "Nos vemos manana en {{hotel_name}}",
                  "body": "Hola {{guest_name}},\n\nUn breve recordatorio de que le "
                          "esperamos manana, {{event_date}}. Referencia {{ref}}."},
            "pt": {"subject": "Ate amanha no {{hotel_name}}",
                  "body": "Ola {{guest_name}},\n\nUm lembrete de que o esperamos amanha, "
                          "{{event_date}}. Referencia {{ref}}."},
        },
        "table": {
            "en": {"subject": "Your table at {{room_type_name}}",
                  "body": "Hello {{guest_name}},\n\nLooking forward to seeing you at "
                          "{{room_type_name}} on {{event_date}} at {{event_time}}. "
                          "Reference {{ref}}."},
        },
        "spa": {
            "en": {"subject": "Your {{room_type_name}} appointment",
                  "body": "Hello {{guest_name}},\n\nA quick reminder of your "
                          "{{room_type_name}} on {{event_date}} at {{event_time}}. Please "
                          "arrive 10 minutes early. Reference {{ref}}."},
        },
        "experience": {
            "en": {"subject": "{{room_type_name}} is coming up",
                  "body": "Hello {{guest_name}},\n\n{{room_type_name}} is on "
                          "{{event_date}} at {{event_time}} - we cannot wait. Reference "
                          "{{ref}}."},
        },
    },
}


@dataclass
class LandedBooking:
    """One booking Loop B has to act on, from whichever origin it came from."""

    ref: str
    kind: str                        # room | table | spa | experience
    origin: str                      # pms | front_desk
    guest_name: str = "Guest"
    guest_email: str = ""
    guest_phone: str = ""
    guest_country: str = ""
    booking_language: str = ""
    correspondence_language: str = ""   # set when origin is front_desk - see design decision 8
    event_date: str = ""              # checkin (room) or the event's own date
    event_time: str = ""              # "" for room (reminder time comes from config)
    room_type_name: str = ""
    total_eur: float = 0.0
    needs_confirmation: bool = True
    needs_reminder: bool = True


def guest_language(booking: LandedBooking, settings: Settings) -> str:
    """Pick the reply language - "follow the guest, not the passport".

    See docs/how-it-works.md design decision 8: a booking attached to an
    inbound item (an upsell accepted over email, for instance) prefers the
    language the guest actually corresponded in over anything recorded on the
    booking, then phone/country, then the hotel's default.
    """
    if booking.correspondence_language:
        return booking.correspondence_language
    guess = detect_language(phone=booking.guest_phone, country=booking.guest_country,
                            booking_language=booking.booking_language, settings=settings)
    return str(guess)


def _vars(booking: LandedBooking, ref_label: str, settings: Settings) -> dict:
    # No "signoff" / team-name entry on purpose: these templates are only
    # ever sent by email (`_channel_and_to` falls back to WhatsApp only when
    # there is no guest email), and `core.adapters.base.Email.with_signature`
    # appends `knowledge/signature.md` to every outbound email automatically -
    # a sign-off written into the template here would show up twice (and,
    # for a hotel whose name already starts with "The", as a literal
    # "The The <name> team"). See docs/how-it-works.md design decision 7.
    return {
        "hotel_name": settings.hotel.name, "guest_name": booking.guest_name or "Guest",
        "room_type_name": booking.room_type_name or booking.kind, "ref": ref_label,
        "event_date": booking.event_date, "event_time": booking.event_time,
    }


def _template_for(event: str, kind: str, lang: str) -> dict[str, str] | None:
    by_kind = TEMPLATES.get(event, {}).get(kind)
    if not by_kind:
        return None
    return by_kind.get(lang) or by_kind.get("en")


def plan_confirmation(booking: LandedBooking, settings: Settings, lang: str) -> dict | None:
    """What to send, and how carefully, for one landed booking. Pure function.

    Returns ``None`` when nobody needs to be told anything new (a
    front_desk-origin booking, whose confirmation was already the approved
    guest-facing reply). Otherwise a plain dict: ``status`` is
    ``needs_human`` above the configured value threshold (design: "a booking
    worth more than this awaits a human's send") or ``pending_review``
    otherwise - either way a human still approves it in shadow mode, this
    only decides which queue bucket it lands in.
    """
    if not booking.needs_confirmation:
        return None
    template = _template_for("confirmation", booking.kind, lang)
    if template is None:
        return None
    threshold = float(settings.agent_get("confirmations.hold_for_review_over_eur", 1500))
    over_threshold = booking.total_eur > threshold
    status = "needs_human" if over_threshold else "pending_review"
    currency = settings.hotel.currency
    reason = (f"booking value {currency} {booking.total_eur:.0f} is over the "
             f"{currency} {threshold:.0f} review threshold" if over_threshold
             else "routine confirmation")
    v = _vars(booking, booking.ref, settings)
    return {
        "status": status, "reason": reason, "language": lang,
        "subject": render_string(template["subject"], v),
        "body": render_string(template["body"], v),
    }


_ROOM_OFFSET = re.compile(r"^t-(\d+)d-(\d{2}):(\d{2})$")
_HOUR_OFFSET = re.compile(r"^t-(\d+)h$")


def _offset_datetime(offset: str, event_dt: datetime) -> datetime:
    """Turn one offset string into an absolute datetime, relative to the event.

    ``t-<N>d-<HH:MM>`` - N days before the event's date, at a fixed clock
    time (the room reminder: "the evening before", not "24 hours before" -
    those differ for a morning arrival). ``t-<N>h`` - N hours before the
    event's own date+time. ``t-morning`` - 08:00 on the day of the event.
    Naive local time throughout - see docs/how-it-works.md for the
    simplification this repo makes here.
    """
    m = _ROOM_OFFSET.match(offset)
    if m:
        days, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime.combine(event_dt.date() - timedelta(days=days), time(hh, mm))
    if offset == "t-morning":
        return datetime.combine(event_dt.date(), time(8, 0))
    m = _HOUR_OFFSET.match(offset)
    if m:
        return event_dt - timedelta(hours=int(m.group(1)))
    raise ValueError(f"unknown reminder offset '{offset}'")


def _event_datetime(booking: LandedBooking) -> datetime | None:
    if not booking.event_date:
        return None
    try:
        d = date.fromisoformat(booking.event_date[:10])
    except ValueError:
        return None
    if booking.event_time:
        try:
            hh, mm = (int(x) for x in booking.event_time.split(":")[:2])
            return datetime.combine(d, time(hh, mm))
        except ValueError:
            pass
    return datetime.combine(d, time(12, 0))


def plan_reminders(booking: LandedBooking, settings: Settings, lang: str,
                   now: datetime) -> list[dict]:
    """Every reminder this booking still needs, oldest first. Pure function.

    A slot whose due time has already passed is dropped - "lapsed" in
    docs/how-it-works.md's flowchart - rather than sent late or queued for a
    human to notice was pointless.
    """
    if not booking.needs_reminder:
        return []
    if booking.kind == "table" and not bool(
            settings.agent_get("confirmations.restaurant_reminder_enabled", False)):
        return []   # most restaurant systems send their own SMS reminder
    event_dt = _event_datetime(booking)
    if event_dt is None:
        return []
    template = _template_for("reminder", booking.kind, lang)
    if template is None:
        return []

    if booking.kind == "room":
        offsets = [settings.agent_get("confirmations.room_reminder", "t-1d-17:00")]
    else:
        offsets = list(settings.agent_get(
            "confirmations.ancillary_reminder_offsets", ["t-24h", "t-morning", "t-2h"]))

    out: list[dict] = []
    v = _vars(booking, booking.ref, settings)
    for offset in offsets:
        try:
            due = _offset_datetime(offset, event_dt)
        except ValueError:
            continue
        if due < now:
            continue   # lapsed - the slot (or the event itself) has already passed
        out.append({
            "offset_label": offset, "due_at": due.isoformat(timespec="minutes"),
            "language": lang, "subject": render_string(template["subject"], v),
            "body": render_string(template["body"], v),
        })
    return out


# --------------------------------------------------------------------------
# impure: talking to the PMS and the store
# --------------------------------------------------------------------------
def _from_reservation(res: Any) -> LandedBooking:
    return LandedBooking(
        ref=res.external_ref or res.id, kind="room", origin="pms",
        guest_name=res.guest.full_name or "Guest", guest_email=res.guest.email,
        guest_phone=res.guest.phone, guest_country=res.guest.country,
        booking_language=res.guest.language, event_date=res.check_in,
        room_type_name=res.room_type_name or res.room_type_id, total_eur=float(res.total),
        needs_confirmation=True, needs_reminder=True)


_STORE_EXT_TABLES = {
    "room": ("room_bookings", "room_type", "checkin"),
    "table": ("table_bookings", None, "date"),
    "spa": ("spa_bookings", "treatment", "date"),
    "experience": ("experience_bookings", None, "date"),
}


def _from_store_row(kind: str, row: dict) -> LandedBooking:
    _, name_col, date_col = _STORE_EXT_TABLES[kind]
    return LandedBooking(
        ref=row["ref"], kind=kind, origin="front_desk",
        guest_name=row.get("guest_name") or "Guest",
        event_date=row.get(date_col, ""), event_time=row.get("time", ""),
        room_type_name=(row.get(name_col) or row.get("session_slug") or kind)
        if name_col or kind == "experience" else kind,
        total_eur=float(row.get("total_eur") or row.get("price_eur") or 0.0),
        correspondence_language="",   # resolved from the source item, see below
        needs_confirmation=False,     # Loop A's own approved reply already told the guest
        needs_reminder=True)


def collect_landed_bookings(settings: Settings, store: Store) -> list[LandedBooking]:
    """Every booking Loop B has not seen before, from both origins.

    Dedup is `store.upsert_unique("landed_booking", ...)` keyed
    `<kind>:<ref>` - see docs/how-it-works.md idempotency notes. Only
    genuinely new bookings are returned; a re-run is a no-op.
    """
    out: list[LandedBooking] = []

    from core.adapters import get_pms
    try:
        pms = get_pms(settings)
        for res in pms.list_reservations("0000-01-01", "9999-12-31"):
            key = f"room:{res.external_ref or res.id}"
            _, created = store.upsert_unique("landed_booking", key, source="pms")
            if created:
                out.append(_from_reservation(res))
    except Exception:  # noqa: BLE001 - a broken PMS must not stop the front-desk booking scan
        pass

    for kind, (table, _, _) in _STORE_EXT_TABLES.items():
        rows = store.db.execute(f"SELECT * FROM {table} ORDER BY created_at ASC").fetchall()
        for r in rows:
            row = dict(r)
            key = f"{kind}:{row['ref']}"
            _, created = store.upsert_unique("landed_booking", key, source="front_desk")
            if not created:
                continue
            booking = _from_store_row(kind, row)
            item = store.get_item(row.get("item_id") or "")
            if item is not None:
                booking.correspondence_language = _detect_correspondence_language(item)
            out.append(booking)
    return out


def _detect_correspondence_language(item: Any) -> str:
    """The language the guest actually wrote in, from their original message.

    The draft schema (prompts/schemas/draft.json) does not carry the
    triage-detected language back onto the item, so this re-detects it from
    the same text with the same heuristic (core.i18n) rather than guessing
    from the booking record - see docs/how-it-works.md design decision 8.
    """
    body = (item.payload or {}).get("body", "")
    if not body:
        return ""
    return str(detect_language(text=body))


def _channel_and_to(booking: LandedBooking) -> tuple[str, str]:
    if booking.guest_email:
        return "email", booking.guest_email
    if booking.guest_phone:
        return "whatsapp", booking.guest_phone
    return "email", ""


def _queue(store: Store, *, kind: str, unique_key: str, channel: str, to: str,
          plan: dict) -> None:
    item, created = store.upsert_unique(
        kind, unique_key, source="confirmations",
        payload={"channel": channel, "to": to, "kind": kind})
    if not created:
        return
    store.set_fields(item.id, draft={**plan, "sender_name": "Front Desk AI",
                                     "channel": channel, "to": to})
    store.transition(item.id, plan["status"], actor="agent",
                     detail={"reason": plan.get("reason", "")})


def process_new_bookings(settings: Settings, store: Store, *,
                         now: datetime | None = None) -> dict:
    """Run one pass of Loop B: collect, plan, queue. No model call.

        python3 tools/run.py --confirmations

    Returns stats for the run summary: how many bookings landed, how many
    confirmations and reminders were queued, and how many reminder slots had
    already lapsed.
    """
    now = now or datetime.utcnow()
    stats = {"bookings": 0, "confirmations_queued": 0, "reminders_queued": 0, "lapsed": 0}
    for booking in collect_landed_bookings(settings, store):
        stats["bookings"] += 1
        lang = guest_language(booking, settings)
        channel, to = _channel_and_to(booking)

        confirmation = plan_confirmation(booking, settings, lang)
        if confirmation is not None:
            _queue(store, kind="confirmation", unique_key=f"confirm:{booking.kind}:{booking.ref}",
                  channel=channel, to=to, plan=confirmation)
            stats["confirmations_queued"] += 1

        reminders = plan_reminders(booking, settings, lang, now)
        offsets_planned = 0
        if booking.kind == "room":
            offsets_planned = 1
        elif booking.kind != "table" or bool(
                settings.agent_get("confirmations.restaurant_reminder_enabled", False)):
            offsets_planned = len(settings.agent_get(
                "confirmations.ancillary_reminder_offsets", ["t-24h", "t-morning", "t-2h"]))
        stats["lapsed"] += max(0, offsets_planned - len(reminders))
        for reminder in reminders:
            _queue(store, kind="reminder",
                  unique_key=f"remind:{booking.kind}:{booking.ref}:{reminder['offset_label']}",
                  channel=channel, to=to, plan={**reminder, "status": "pending_review",
                                               "reason": "scheduled reminder"})
            stats["reminders_queued"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI, see tools/run.py
    import argparse
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from core.config import ConfigError, load_settings

    ap = argparse.ArgumentParser(description="Run Loop B (confirmations + reminders) once")
    ap.add_argument("--provider", default=None)
    args = ap.parse_args(argv)
    try:
        settings = load_settings(provider=args.provider)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    store = Store(settings)
    try:
        stats = process_new_bookings(settings, store)
        print(f"{stats['bookings']} booking(s) seen, "
             f"{stats['confirmations_queued']} confirmation(s) queued, "
             f"{stats['reminders_queued']} reminder(s) queued, "
             f"{stats['lapsed']} lapsed ({settings.mode})")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
