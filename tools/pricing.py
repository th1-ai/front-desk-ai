"""tools/pricing.py - pure functions: room rates, spa matching, table rules.

No I/O, no store, no settings object mutated. Every function here takes plain
values (or the small config dicts loaded from `config/agent.yaml`) and
returns a plain value, so `tests/test_frontdesk_pricing.py` can check the
maths without a database or a fixture file. `tools/booking.py` is the only
caller - it does the I/O, this does the arithmetic.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from typing import Any

DEFAULT_SEASON = [0.85, 0.85, 0.90, 1.00, 1.10, 1.25, 1.35, 1.35, 1.15, 1.00, 0.90, 0.95]

# Config entries (spa menu items, experience sessions) print this note once
# per label the first time a caller falls back to the deprecated key, so a
# hotelier notices without a stack trace or a silently-broken price.
_PRICE_EUR_WARNED: set[str] = set()


class UnknownRoomType(ValueError):
    """Raised with the valid list, so the caller can offer it to the guest."""


class UnknownTreatment(ValueError):
    """No spa menu entry matched, even loosely."""


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def room_type_list(room_types: dict) -> str:
    """``slug (Display Name)`` for every configured room type, one per entry -
    what the triage prompt shows the model, and what an error message offers
    a guest."""
    return ", ".join(f"{slug} ({cfg.get('name', slug)})" for slug, cfg in room_types.items())


def match_room_type(query: str, room_types: dict) -> str:
    """Fuzzy-match a guest's (or the model's) words to a room-type slug.

    ``room_types`` is ``config/agent.yaml: rooms.room_types`` - a mapping of
    slug -> ``{name, base_rate, max_occupancy}``. Guests, and the models
    extracting from them, rarely say the internal slug (``garden``) - they
    say the property's own display name (``Garden Room``) or something close
    to it. Order, mirroring :func:`match_treatment`'s approach for the spa
    menu: exact slug (case-insensitive), then exact display name
    (case-insensitive), then a substring match either way against the
    display name, then a substring/prefix match against the slug with
    hyphens treated as spaces (so "Four Poster" reaches the ``four-poster``
    slug). Raises :class:`UnknownRoomType` naming every slug alongside its
    display name when nothing matches, so the caller can offer the real list
    instead of guessing.
    """
    needle = (query or "").strip().lower()
    if not needle:
        raise UnknownRoomType("no room type given. Known: " + room_type_list(room_types))

    for slug in room_types:
        if slug.lower() == needle:
            return slug
    for slug, cfg in room_types.items():
        if str(cfg.get("name", "")).strip().lower() == needle:
            return slug
    for slug, cfg in room_types.items():
        name = str(cfg.get("name", "")).strip().lower()
        if name and (needle in name or name in needle):
            return slug
    for slug in room_types:
        norm = slug.lower().replace("-", " ")
        if norm and (norm in needle or needle in norm
                    or norm.startswith(needle) or needle.startswith(norm)):
            return slug
    for slug, cfg in room_types.items():
        name = str(cfg.get("name", "")).strip().lower()
        if name and (name.startswith(needle) or needle.startswith(name)):
            return slug
    raise UnknownRoomType(
        f"unknown room type '{query}'. Known: " + room_type_list(room_types))


def nightly_rate(room_type: str, day: date, room_types: dict, *,
                 season: list[float] | None = None, weekend_multiplier: float = 1.08,
                 weekend_days: tuple[int, ...] = (4, 5)) -> float:
    """One night's rate: base x season[month] x weekend, rounded to the nearest 5.

    ``room_types`` is ``config/agent.yaml: rooms.room_types`` - a mapping of
    id -> ``{name, base_rate, max_occupancy}``. Raises :class:`UnknownRoomType`
    for an id not in that mapping.
    """
    cfg = room_types.get(room_type)
    if cfg is None:
        raise UnknownRoomType(
            f"unknown room type '{room_type}'. Known: {', '.join(sorted(room_types))}")
    season = season or DEFAULT_SEASON
    base = float(cfg["base_rate"])
    factor = season[day.month - 1] * (weekend_multiplier if day.weekday() in weekend_days else 1.0)
    return round(base * factor / 5) * 5


def stay_total(room_type: str, checkin: str, checkout: str, room_types: dict, **kwargs) -> float:
    """Sum of :func:`nightly_rate` over every night of the stay."""
    start, end = _parse_date(checkin), _parse_date(checkout)
    if end <= start:
        raise ValueError("checkout must be after checkin")
    total = 0.0
    day = start
    while day < end:
        total += nightly_rate(room_type, day, room_types, **kwargs)
        day = date.fromordinal(day.toordinal() + 1)
    return total


def nights_between(checkin: str, checkout: str) -> int:
    return (_parse_date(checkout) - _parse_date(checkin)).days


def config_price(item: dict, *, label: str) -> float:
    """Read a hotelier-set price out of a config entry (a spa menu item, an
    experience session) - the ``price`` key, currency-neutral since the
    amount is always formatted per ``hotel.currency`` at display time, never
    hardcoded to EUR.

    ``price_eur`` (the old field name, still accepted in
    `config/agent.yaml` and `fixtures/hotel/experiences.json`) works for one
    more release as a deprecated alias, so an existing config keeps working
    unedited - but this prints a one-line note to stderr, once per
    ``label``, so a hotelier notices and renames it on their own time.
    """
    if "price" in item:
        return float(item["price"])
    if "price_eur" in item:
        if label not in _PRICE_EUR_WARNED:
            print(f"note: '{label}' uses the deprecated config key 'price_eur' - "
                 "rename it to 'price' (the amount is already shown in hotel.currency, "
                 "not always EUR).", file=sys.stderr)
            _PRICE_EUR_WARNED.add(label)
        return float(item["price_eur"])
    raise KeyError(f"'{label}' is missing a 'price' field")


@dataclass
class SpaMatch:
    treatment_id: str
    title: str
    price_eur: float
    party_size: int
    flat_price: bool
    total_eur: float


def match_treatment(query: str, menu: list[dict], party_size: int | None = None) -> SpaMatch:
    """Fuzzy-match a guest's words against the spa menu.

    Order: exact title (case-insensitive), then a substring match either way,
    then the per-item ``hints`` keywords. Raises :class:`UnknownTreatment`
    when nothing matches, so the caller can ask the guest to pick from the
    list instead of guessing.
    """
    needle = (query or "").strip().lower()
    if not needle:
        raise UnknownTreatment("no treatment named")

    def _as_match(item: dict) -> SpaMatch:
        size = party_size or int(item.get("party_size", 1))
        flat = bool(item.get("flat_price"))
        price = config_price(item, label=item.get("title") or item.get("id") or "spa item")
        total = price if flat else price * size
        return SpaMatch(treatment_id=item["id"], title=item["title"], price_eur=price,
                        party_size=size, flat_price=flat, total_eur=total)

    for item in menu:
        if item["title"].strip().lower() == needle:
            return _as_match(item)
    for item in menu:
        title = item["title"].strip().lower()
        if needle in title or title in needle:
            return _as_match(item)
    for item in menu:
        for hint in item.get("hints", []):
            if hint.lower() in needle:
                return _as_match(item)
    raise UnknownTreatment(
        f"could not match '{query}' to a treatment. Menu: "
        + ", ".join(i["title"] for i in menu))


def is_restaurant_closed(day: date, closed_weekdays: list[int]) -> bool:
    return day.weekday() in (closed_weekdays or [])


def is_large_group(*, pax: int | None = None, rooms: int | None = None,
                   large_group_pax: int = 15, large_group_rooms: int = 6) -> bool:
    """True when a room request is big enough to always need a human."""
    if pax is not None and pax >= large_group_pax:
        return True
    if rooms is not None and rooms >= large_group_rooms:
        return True
    return False


def is_large_party(party_size: int, large_party_size: int = 10) -> bool:
    return party_size >= large_party_size


def format_ref(prefix: str, seed: int) -> str:
    """A short, human-readable booking reference. ``seed`` should vary per call
    (e.g. a row count or a random int) - callers own how they generate it."""
    return f"{prefix}-{seed:04d}"
