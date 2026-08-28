#!/usr/bin/env python3
"""tools/doctor.py - is Front Desk AI configured and reachable right now?

    make doctor
    python3 tools/doctor.py

Runs the generic core.doctor checks (python, deps, config, .env, hotel
identity, mode, llm provider, every adapter, the store, knowledge) plus the
checks specific to this agent: room/spa/restaurant configuration, the two
prompt tasks, and whether the Specialist Booking catalogue is in place when
that sub-agent is turned on. Exits 0 when everything passed, 1 when a FAIL
line needs fixing. Never a traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.doctor import Check, FAIL, PASS, WARN, print_table, run_checks  # noqa: E402


def check_rooms_and_spa(settings: Settings) -> list[Check]:
    out = []
    room_types = settings.agent_get("rooms.room_types", {})
    if not room_types:
        out.append(Check("room types", FAIL, "no rooms.room_types in config/agent.yaml",
                         "Copy config/agent.example.yaml to config/agent.yaml - it ships "
                         "with four sample room types."))
    else:
        out.append(Check("room types", PASS, f"{len(room_types)}: {', '.join(room_types)}"))
    menu = settings.agent_get("spa.menu", [])
    out.append(Check("spa menu", PASS if menu else WARN,
                     f"{len(menu)} treatment(s)" if menu else "no spa.menu configured - "
                     "spa bookings will always need a human"))
    restaurant = settings.agent_get("restaurant", {})
    out.append(Check("restaurant", PASS if restaurant.get("name") else WARN,
                     restaurant.get("name", "not configured")))
    return out


def check_prompts() -> Check:
    missing = [p for p in ("prompts/triage.md", "prompts/draft.md", "prompts/coach-suggestion.md",
                           "prompts/schemas/triage.json", "prompts/schemas/draft.json")
              if not (REPO_ROOT / p).is_file()]
    if missing:
        return Check("prompts", FAIL, f"missing {', '.join(missing)}",
                     "These ship with the repo - restore them from git.")
    return Check("prompts", PASS, "triage + draft + coach-suggestion present")


def check_specialist_booking(settings: Settings) -> Check:
    enabled = bool(settings.agent_get("subagents.specialist_booking.enabled", False))
    fixture = REPO_ROOT / "fixtures" / "hotel" / "experiences.json"
    if not enabled:
        return Check("specialist booking", WARN, "off - experience enquiries route back to "
                     "front_desk with a note", "Turn it on in config/agent.yaml once "
                     "fixtures/hotel/experiences.json reflects your real sessions.")
    if not fixture.is_file():
        return Check("specialist booking", FAIL, "enabled but fixtures/hotel/experiences.json "
                     "is missing", "Add the file (see workflows/20-specialist-booking.md) or "
                     "turn subagents.specialist_booking.enabled back off.")
    return Check("specialist booking", PASS, "on, catalogue present")


def check_coach(settings: Settings) -> Check:
    enabled = bool(settings.agent_get("subagents.coach.enabled", True))
    rules = REPO_ROOT / "knowledge" / "rules.md"
    if not enabled:
        return Check("coach", WARN, "off")
    if not rules.exists():
        return Check("coach", PASS, "on, no accepted proposals applied yet")
    return Check("coach", PASS, f"on, {rules.stat().st_size} bytes in {rules.name}")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        checks = run_checks(None) + [Check("config", FAIL, str(exc),
                                           "Fix config/hotel.yaml or config/agent.yaml.")]
        return print_table(checks, title="Front Desk AI - doctor")

    checks = run_checks(settings, extra=[check_rooms_and_spa, check_specialist_booking,
                                         check_coach])
    checks.append(check_prompts())
    return print_table(checks, title="Front Desk AI - doctor")


if __name__ == "__main__":
    raise SystemExit(main())
