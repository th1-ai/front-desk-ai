#!/usr/bin/env python3
"""tools/run.py - Front Desk AI's two independent loops.

    python3 tools/run.py --once                  # Loop A: the inbox (default)
    python3 tools/run.py --watch                  # Loop A, looping on a timer
    python3 tools/run.py --once --confirmations   # Loop B: confirmations + reminders
    python3 tools/run.py --once --dry-run
    python3 tools/run.py --once --limit 5
    python3 tools/run.py --once --provider mock

Loop A fetches unread email and new chat messages, triages, books (preview
only), drafts, and queues every one for review - see
docs/how-it-works.md and workflows/10-front-desk.md. The Specialist Booking
lane and the WhatsApp channel share this same loop (a triage field and a
payload field, not a separate pass) - see workflows/20-specialist-booking.md
and workflows/21-whatsapp.md.

Loop B (``--confirmations``) is a different job on a different clock: it
never calls a model, and it is what schedules the confirmation and reminder
messages for a booking that landed with nobody having written in - see
workflows/15-confirmations.md.

Exit codes: 0 ok, 3 waiting on an `interactive` answer (see the message),
1 a real error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_messaging  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive  # noqa: E402
from core.log import Run, get_logger, summary_line  # noqa: E402
from core.store import Store, StoreError  # noqa: E402

import store_ext  # noqa: E402
from confirmations import process_new_bookings  # noqa: E402
from engine import chat_to_dict, email_to_dict, process_message  # noqa: E402

log = get_logger("run")
EXPERIENCES_FIXTURE = REPO_ROOT / "fixtures" / "hotel" / "experiences.json"


def _seed_experiences(store: Store) -> None:
    if not EXPERIENCES_FIXTURE.exists():
        return
    sessions = json.loads(EXPERIENCES_FIXTURE.read_text(encoding="utf-8"))
    store_ext.seed_experience_sessions(store, sessions)


def one_pass(settings, store, *, limit: int, provider: str | None) -> tuple[int, dict]:
    """Loop A: fetch email + chat, triage, book (preview), draft, queue."""
    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0, "skipped": 0}
    with Run("front-desk", settings, store) as run:
        _seed_experiences(store)

        # No pre-filter on `already_processed` here on purpose: that check only
        # tells you the row exists, not that triage+draft finished, so it would
        # permanently skip an item still waiting on an `interactive` answer
        # (upsert_item creates the row before the model call that can pend).
        # `process_message` is its own idempotency check (`if item.intent:
        # return item, False`) - correct whether the item is brand new,
        # already fully handled, or stuck mid-interactive-prompt.
        email = get_email(settings)
        messages = email.fetch_unread(limit=limit)
        for msg in messages:
            try:
                item, did_work = process_message(settings, store, "email", email_to_dict(msg),
                                                 channel="email", provider=provider)
            except LLMPendingInteractive as exc:
                run.stats = dict(stats)
                print(str(exc))
                return 3, stats
            _tally(stats, item, did_work)

        messaging = get_messaging(settings)
        chats = messaging.fetch_new(limit=limit)
        for msg in chats:
            try:
                item, did_work = process_message(settings, store, "whatsapp", chat_to_dict(msg),
                                                 channel="whatsapp", provider=provider)
            except LLMPendingInteractive as exc:
                run.stats = dict(stats)
                print(str(exc))
                return 3, stats
            _tally(stats, item, did_work)

        reaped = store.reap_stuck_sending()
        if reaped:
            log.warn("reaped stuck sends", count=len(reaped))
        run.stats = dict(stats)
    return 0, stats


def _tally(stats: dict, item, did_work: bool) -> None:
    if not did_work:
        stats["skipped"] += 1
        return
    stats["processed"] += 1
    stats["drafted"] += 1
    if item.review_status == "needs_human":
        stats["needs_human"] += 1
    log.info("queued", item_id=item.id, intent=item.intent, status=item.review_status)


def confirmations_pass(settings, store) -> tuple[int, dict]:
    """Loop B: no model call - see tools/confirmations.py."""
    with Run("confirmations", settings, store) as run:
        stats = process_new_bookings(settings, store)
        run.stats = dict(stats)
    return 0, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="run a single pass (default)")
    mode_group.add_argument("--watch", action="store_true",
                            help="keep running on the configured interval")
    parser.add_argument("--confirmations", action="store_true",
                        help="run Loop B (confirmations + reminders) instead of Loop A")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything, write nothing, even in live mode")
    parser.add_argument("--limit", type=int, default=20, help="max messages per pass")
    parser.add_argument("--provider", default=None,
                        help="override llm.provider for this run")
    parser.add_argument("--poll-seconds", type=int, default=None,
                        help="override the --watch interval (default: agent.yaml or 300)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(provider=args.provider, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    # --dry-run is a rehearsal: compute everything, write nothing - not even to
    # this repo's own data/agent.db. An ephemeral in-memory database gives every
    # tool call somewhere real to write during the pass (so the code path is
    # exercised exactly as normal) while guaranteeing nothing lands on disk and
    # nothing from one --dry-run pass can collide with the next one (no rows,
    # no IntegrityError, ever - each pass starts from empty). See
    # factory/workflows/build-repo.md section 5.
    store = Store(settings, path=":memory:" if settings.dry_run else None)
    store_ext.ensure_schema(store)
    key = "confirmations" if args.confirmations else "triage"

    def run_once() -> tuple[int, dict]:
        if args.confirmations:
            return confirmations_pass(settings, store)
        return one_pass(settings, store, limit=args.limit, provider=args.provider)

    try:
        if args.watch:
            default_seconds = 900 if args.confirmations else 300
            poll_seconds = args.poll_seconds or int(
                settings.agent_get(f"schedule.{key}_seconds", default_seconds))
            while True:
                code, stats = run_once()
                print(summary_line(stats, settings.mode) if not args.confirmations
                     else _confirmations_summary(stats, settings.mode))
                if code != 0:
                    return code
                time.sleep(poll_seconds)
        code, stats = run_once()
        print(summary_line(stats, settings.mode) if not args.confirmations
             else _confirmations_summary(stats, settings.mode))
        return code
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def _confirmations_summary(stats: dict, mode: str) -> str:
    return (f"{stats.get('bookings', 0)} booking(s) seen, "
           f"{stats.get('confirmations_queued', 0)} confirmation(s) queued, "
           f"{stats.get('reminders_queued', 0)} reminder(s) queued, "
           f"{stats.get('lapsed', 0)} lapsed ({mode})")


if __name__ == "__main__":
    raise SystemExit(main())
