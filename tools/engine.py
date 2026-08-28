"""tools/engine.py - Loop A: one guest message in, one queued reply out.

Deterministic decisioning, LLM for language (ARCHITECTURE.md section 1): the
only two model calls are ``triage`` and ``draft`` (always through
``core.llm.complete`` with a JSON schema). Everything else - which lane owns
the message, whether a booking is valid, whether a human must see it before
it goes out - is a plain rule or a lookup, not something the model decides.

Shared by ``tools/run.py`` (the real loop) and ``tools/demo.py`` (the
zero-credential walkthrough), so both exercise exactly the same code path.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.adapters.base import ChatMessage, EmailMessage
from core.config import Settings
from core.llm import LLMResult, LLMSchemaError, complete
from core.store import Item, Store
from core.templates import build_prompt

import store_ext
from booking import BookingOutcome, compute_pending
from pricing import room_type_list

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "schemas"
LANE_NAME = {"front_desk": "Front Desk AI", "specialist": "Specialist Booking AI"}


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


TRIAGE_SCHEMA = _schema("triage")
DRAFT_SCHEMA = _schema("draft")


# --------------------------------------------------------------------------
# turning an adapter message into the shape prompts and the store use
# --------------------------------------------------------------------------
def email_to_dict(msg: EmailMessage) -> dict:
    return {"id": msg.id, "from": msg.from_email, "from_name": msg.from_name,
           "subject": msg.subject, "body": msg.body_text, "received_at": msg.received_at,
           "reservation_ref": _find_ref(msg.subject + " " + msg.body_text)}


def chat_to_dict(msg: ChatMessage) -> dict:
    return {"id": msg.id, "chat_id": msg.chat_id, "from_name": msg.from_name,
           "from_number": msg.from_number, "subject": "", "body": msg.text,
           "received_at": msg.sent_at,
           "reservation_ref": _find_ref(msg.text)}


def _find_ref(text: str) -> str | None:
    """A cheap, deterministic scan for a booking reference already in the
    message (``RES-1234``, ``TBL-1234``, ``SPA-1234``, ``EXP-1234``) - so a
    guest fact ("my wife is allergic to nuts, ref RES-4821") can be filed
    against the right booking without asking the model to copy digits."""
    import re
    m = re.search(r"\b(RES|TBL|SPA|EXP)-\d{3,5}\b", text or "", re.I)
    return m.group(0).upper() if m else None


# --------------------------------------------------------------------------
# the two model calls
# --------------------------------------------------------------------------
def run_triage(settings: Settings, store: Store, item: Item, msg: dict, *,
              channel: str, provider: str | None = None) -> dict:
    restaurant_name = settings.agent_get("restaurant.name", "the restaurant")
    room_types = room_type_list(settings.agent_get("rooms.room_types", {})) or "none configured"
    prompt = build_prompt("triage", settings=settings, item=msg, fixture_id=item.external_id,
                          channel=channel, restaurant_name=restaurant_name,
                          room_types=room_types)
    result: LLMResult = complete("triage", prompt, TRIAGE_SCHEMA, settings=settings,
                                 provider=provider, store=store, item_id=item.id,
                                 fixture_id=item.external_id)
    data = result.data or {}
    store.set_fields(item.id, intent=data.get("intent"),
                     confidence=float(data.get("confidence", 0.0)))
    return data


def run_draft(settings: Settings, store: Store, item: Item, triage_data: dict,
             pending: BookingOutcome, *, channel: str,
             provider: str | None = None) -> dict:
    draft_item = {**triage_data, "channel": channel, "booking_outcome": pending.as_dict()}
    prompt = build_prompt("draft", settings=settings, item=draft_item, fixture_id=item.external_id)
    result: LLMResult = complete("draft", prompt, DRAFT_SCHEMA, settings=settings,
                                 provider=provider, store=store, item_id=item.id,
                                 fixture_id=item.external_id)
    return result.data or {}


# --------------------------------------------------------------------------
# deterministic decisions
# --------------------------------------------------------------------------
def apply_lane_gate(triage_data: dict, settings: Settings) -> dict:
    """Force experience-shaped messages back to front_desk when the
    Specialist sub-agent is off. Mutates and returns the same dict."""
    if triage_data.get("lane") == "specialist" and not settings.agent_get(
            "subagents.specialist_booking.enabled", False):
        triage_data["lane"] = "front_desk"
        triage_data["intent"] = "question"
        missing = list(triage_data.get("missing_info") or [])
        missing.append("experience bookings are not enabled on this property yet")
        triage_data["missing_info"] = missing
    return triage_data


def apply_language_gate(triage_data: dict, settings: Settings) -> dict | None:
    """Force the reply language to the hotel's own default when the guest
    wrote in a language this property does not list in `hotel.languages`
    (`config/hotel.yaml`) - nobody on the team could check a reply in a
    language nobody configured, so this always escalates instead of letting
    the draft step answer fluently in a language nobody can verify. Mutates
    `triage_data["language"]` in place; returns the escalation block when it
    fires, else `None`. See docs/how-it-works.md design decision 10.
    """
    lang = str(triage_data.get("language") or "").strip().lower()
    supported = [str(x).strip().lower() for x in settings.hotel.languages]
    if not lang or lang in supported:
        return None
    reason = f"guest wrote in {lang}, not in hotel.languages ({', '.join(supported)})"
    triage_data["language"] = settings.hotel.default_language
    return {"category": "missing_info", "reason": reason}


def apply_guardrails(triage_data: dict, pending: BookingOutcome) -> dict | None:
    """Deterministic re-check, on top of whatever the model decided.

    Always escalates a booking preview that needed a human - a large-group
    room request, an oversized restaurant party, or anything else
    `tools/booking.py` flagged - using the SPECIFIC reason it already
    computed (`pending.error` when there is one) rather than a single canned
    line, so a reviewer sees the real cause (unknown room type, a missing
    detail, an oversized party...) instead of a misleading guess. See
    docs/how-it-works.md design decision 9 and knowledge/policies.md.
    """
    if triage_data.get("escalation"):
        return triage_data["escalation"]
    if not pending.needs_human:
        return None
    if pending.error:
        return {"category": "missing_info", "reason": pending.error}
    return {"category": "policy_exception",
           "reason": "Large group or party size needs a person to arrange - see "
                     "knowledge/policies.md."}


def needs_human_for(triage_data: dict, pending: BookingOutcome, draft_data: dict,
                    settings: Settings) -> bool:
    """Plain rule, not a model decision - see docs/safety.md.

    Always true when escalating, always true below `confidence_threshold`,
    always true when a detail is missing or a booking could not be validated,
    always true when `tools/booking.py` itself flagged the booking outcome
    (`pending.needs_human` - a large group, or an intent like `modification`
    that has no deterministic availability check to run at all), and true
    whenever the draft step itself says so.
    """
    threshold = float(settings.agent_get("confidence_threshold", 0.80))
    if bool(draft_data.get("needs_human")):
        return True
    if triage_data.get("escalation"):
        return True
    if float(triage_data.get("confidence", 0.0)) < threshold:
        return True
    if triage_data.get("missing_info"):
        return True
    if pending.needs_human:
        return True
    if not pending.ok and pending.kind != "table":
        # a rejected table booking (closed day) is a normal reply with an
        # alternative offered, not automatically a human matter; anything
        # else that failed validation (bad room type, unmatched treatment,
        # unknown session, oversell) is safer with a person's eyes on it.
        return True
    return False


# --------------------------------------------------------------------------
# the whole pass for one message
# --------------------------------------------------------------------------
def process_message(settings: Settings, store: Store, source: str, msg_dict: dict, *,
                    channel: str, provider: str | None = None) -> tuple[Item, bool]:
    """Triage, book (preview only), draft and queue one inbound message.

    Idempotent: an item that already has both an intent AND a draft was
    handled by an earlier pass and is left untouched (returns ``(item,
    False)``). Checking intent alone is not enough - with ``llm.provider:
    interactive`` the triage call can succeed and set ``intent`` on one run,
    then the draft call pends on the very next line waiting for a second
    answer; without also checking ``draft``, a later run would see the
    intent already set and skip straight past the draft step forever,
    leaving the item stuck at ``new`` with no way to reach the review queue.

    The full triage result is cached on ``item.payload["_triage_cache"]`` the
    first time it succeeds, so that second round trip does not have to ask
    triage all over again (which would burn a fresh, unanswered interactive
    prompt every time and never progress) - it resumes straight into
    booking + draft with the same triage result. Re-applying the gate and
    guardrails to a cached result is safe: both are idempotent on their own
    output. A schema error from either model call queues the item as
    ``needs_human`` with the error recorded rather than guessing or crashing
    the batch.
    """
    external_id = str(msg_dict["id"])
    existing = store.get_by_external(source, external_id)
    fresh_payload = {**msg_dict, "channel": channel}
    if existing is not None and "_triage_cache" in (existing.payload or {}):
        # Preserve the cache across the refresh below - upsert_item overwrites
        # payload whenever it differs from what is stored, and a payload built
        # from msg_dict alone never carries the cache key.
        fresh_payload["_triage_cache"] = existing.payload["_triage_cache"]
    item = store.upsert_item(source, external_id, kind="message", payload=fresh_payload)
    if item.intent and item.draft is not None:
        return item, False

    cached_triage = (item.payload or {}).get("_triage_cache")
    if cached_triage is not None:
        triage_data = cached_triage
    else:
        try:
            triage_data = run_triage(settings, store, item, msg_dict, channel=channel,
                                     provider=provider)
        except LLMSchemaError as exc:
            store.set_fields(item.id, error=str(exc))
            updated = store.transition(item.id, "needs_human", actor="agent",
                                       detail={"error": "triage_schema_error"})
            return updated, True
        item = store.set_fields(item.id, payload={**item.payload, "_triage_cache": triage_data})

    apply_lane_gate(triage_data, settings)
    language_escalation = apply_language_gate(triage_data, settings)
    pending = compute_pending(settings, store, triage_data)
    escalation = apply_guardrails(triage_data, pending) or language_escalation
    if escalation:
        triage_data["escalation"] = escalation

    try:
        draft_data = run_draft(settings, store, item, triage_data, pending, channel=channel,
                               provider=provider)
    except LLMSchemaError as exc:
        store.set_fields(item.id, error=str(exc))
        updated = store.transition(item.id, "needs_human", actor="agent",
                                   detail={"error": "draft_schema_error",
                                          "intent": triage_data.get("intent")})
        return updated, True

    lane = triage_data.get("lane", "front_desk")
    sender_name = LANE_NAME.get(lane, "Front Desk AI")
    store.set_fields(item.id, draft={**draft_data, "sender_name": sender_name,
                                     "channel": channel, "lane": lane,
                                     "pending_booking": pending.as_dict()})

    if triage_data.get("escalation"):
        esc = triage_data["escalation"]
        store_ext.record_escalation(store, item.id, esc["category"], esc["reason"],
                                    draft_data.get("ai_suggested_reply") or "")

    needs_human = needs_human_for(triage_data, pending, draft_data, settings)
    status = "needs_human" if needs_human else "pending_review"
    updated = store.transition(item.id, status, actor="agent",
                               detail={"intent": triage_data.get("intent"), "lane": lane})
    return updated, True
