"""Pure-function tests for tools/pricing.py - no store, no fixtures, no I/O."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest

from pricing import (UnknownRoomType, UnknownTreatment, config_price, is_large_group,
                     is_large_party, is_restaurant_closed, match_room_type, match_treatment,
                     nightly_rate, nights_between, stay_total)

ROOM_TYPES = {"classic": {"name": "Classic Room", "base_rate": 180, "max_occupancy": 2}}
# A property with a display name that reads nothing like its slug (Round-2
# finding 2's exact reproduction: "Garden Room" -> `garden`), plus a
# hyphenated slug to exercise the space/hyphen normalisation.
ROOM_TYPES_MULTI = {
    "courtyard": {"name": "Courtyard Room", "base_rate": 160, "max_occupancy": 2},
    "four-poster": {"name": "Four-Poster Room", "base_rate": 210, "max_occupancy": 2},
    "garden": {"name": "Garden Room", "base_rate": 190, "max_occupancy": 2},
    "rectory-suite": {"name": "The Rectory Suite", "base_rate": 340, "max_occupancy": 4},
}
MENU = [
    {"id": "signature-massage", "title": "Signature Massage 60", "price": 95,
     "party_size": 1, "hints": ["massage"]},
    {"id": "couples-ritual", "title": "Aurora Couples Ritual", "price": 260,
     "party_size": 2, "flat_price": True, "hints": ["couple", "ritual"]},
]


def test_nightly_rate_applies_weekend_multiplier():
    friday = date(2026, 9, 4)      # Friday - a weekend night
    tuesday = date(2026, 9, 1)     # Tuesday - not
    weekend = nightly_rate("classic", friday, ROOM_TYPES, season=[1.0] * 12)
    weekday = nightly_rate("classic", tuesday, ROOM_TYPES, season=[1.0] * 12)
    assert weekend > weekday
    assert weekend == round(180 * 1.08 / 5) * 5


def test_nightly_rate_applies_season_multiplier():
    season = [1.0] * 12
    season[8] = 1.5   # September
    rate = nightly_rate("classic", date(2026, 9, 1), ROOM_TYPES, season=season)
    assert rate == round(180 * 1.5 / 5) * 5


def test_nightly_rate_unknown_room_type_raises_with_the_valid_list():
    with pytest.raises(UnknownRoomType, match="classic"):
        nightly_rate("penthouse", date(2026, 9, 1), ROOM_TYPES)


def test_stay_total_sums_every_night_and_checkout_must_be_after_checkin():
    total = stay_total("classic", "2026-09-01", "2026-09-03", ROOM_TYPES, season=[1.0] * 12)
    assert total == nightly_rate("classic", date(2026, 9, 1), ROOM_TYPES, season=[1.0] * 12) * 2
    with pytest.raises(ValueError):
        stay_total("classic", "2026-09-03", "2026-09-01", ROOM_TYPES)


def test_nights_between():
    assert nights_between("2026-09-01", "2026-09-04") == 3


def test_match_treatment_exact_substring_and_hint():
    assert match_treatment("Signature Massage 60", MENU).treatment_id == "signature-massage"
    assert match_treatment("a signature massage please", MENU).treatment_id == \
        "signature-massage"
    assert match_treatment("something for me and my partner, a ritual", MENU).treatment_id == \
        "couples-ritual"


def test_match_treatment_flat_price_ignores_party_size_in_the_total():
    match = match_treatment("couples ritual", MENU, party_size=2)
    assert match.total_eur == 260   # flat, not 260 x 2


def test_match_treatment_unknown_raises_with_the_menu():
    with pytest.raises(UnknownTreatment, match="Signature Massage 60"):
        match_treatment("a haircut", MENU)


def test_match_room_type_exact_slug_and_exact_display_name():
    assert match_room_type("garden", ROOM_TYPES_MULTI) == "garden"
    assert match_room_type("GARDEN", ROOM_TYPES_MULTI) == "garden"
    assert match_room_type("Garden Room", ROOM_TYPES_MULTI) == "garden"


def test_match_room_type_display_name_case_insensitive_and_near_matches():
    """Regression test for SIMULATION.md Round-2 finding 2: a genuinely valid,
    currently-bookable room ("Garden Room") must resolve to its slug
    (`garden`), not be rejected as "unknown room type" just because the
    guest or the triage model wrote the display name instead of the slug."""
    assert match_room_type("garden room", ROOM_TYPES_MULTI) == "garden"
    assert match_room_type("a garden room please", ROOM_TYPES_MULTI) == "garden"
    assert match_room_type("Four Poster Room", ROOM_TYPES_MULTI) == "four-poster"
    assert match_room_type("four poster", ROOM_TYPES_MULTI) == "four-poster"
    assert match_room_type("the rectory suite", ROOM_TYPES_MULTI) == "rectory-suite"
    assert match_room_type("Courtyard", ROOM_TYPES_MULTI) == "courtyard"


def test_match_room_type_unknown_raises_with_the_valid_list():
    with pytest.raises(UnknownRoomType, match="garden"):
        match_room_type("Presidential Penthouse", ROOM_TYPES_MULTI)


def test_is_restaurant_closed_on_configured_weekday():
    monday = date(2026, 9, 14)
    tuesday = date(2026, 9, 15)
    assert is_restaurant_closed(monday, [0]) is True
    assert is_restaurant_closed(tuesday, [0]) is False


def test_large_group_and_large_party_thresholds():
    assert is_large_group(pax=15, large_group_pax=15) is True
    assert is_large_group(pax=14, large_group_pax=15) is False
    assert is_large_group(rooms=6, large_group_rooms=6) is True
    assert is_large_party(10, large_party_size=10) is True
    assert is_large_party(9, large_party_size=10) is False


def test_config_price_prefers_the_currency_neutral_key():
    assert config_price({"price": 95}, label="signature-massage") == 95.0


def test_config_price_falls_back_to_the_deprecated_key_with_a_note(capsys):
    """Regression test for SIMULATION.md Round-2 finding 4: an existing
    config that still says `price_eur` (config/agent.yaml, or
    fixtures/hotel/experiences.json) must keep working unedited - the old
    key is a deprecated alias, not a breaking change - while printing a
    one-line note so a hotelier notices and renames it."""
    assert config_price({"price_eur": 55}, label="garden-reflexology") == 55.0
    assert "deprecated" in capsys.readouterr().err


def test_config_price_missing_raises():
    with pytest.raises(KeyError, match="signature-massage"):
        config_price({}, label="signature-massage")
