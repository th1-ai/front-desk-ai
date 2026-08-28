#!/usr/bin/env python3
"""tools/demo.py - both loops, on the bundled fixtures, zero credentials.

    make demo
    python3 tools/demo.py

Forces `llm.provider=mock` and `mode=shadow` regardless of config/hotel.yaml,
so this always works on a fresh clone with a blank .env (ARCHITECTURE.md
section 1). Runs against its own database (data/demo/demo.db) and never
touches data/agent.db (that is `make run`'s file), so running it twice always
shows the same result.

Loop B's "is this reminder slot already lapsed" check needs a clock - the
demo pins it to a fixed date instead of the real one, so the walkthrough
reads the same today as it will in five years (real `make run` uses the
real time - see tools/run.py).

Prints one line every check reads for the pass/fail signal:

    DEMO OK - 14 items processed, 14 drafted, 0 sent (shadow)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_messaging  # noqa: E402
from core.config import ConfigError, load_settings, sub_data_dir  # noqa: E402
from core.log import summary_line  # noqa: E402
from core.store import Store  # noqa: E402

import store_ext  # noqa: E402
from confirmations import process_new_bookings  # noqa: E402
from engine import chat_to_dict, email_to_dict, process_message  # noqa: E402
from run import _seed_experiences  # noqa: E402

DEMO_NOW = datetime(2026, 8, 25, 9, 0)   # a fixed clock - see the module docstring


def main() -> int:
    # load_settings(demo=True) forces provider=mock, mode=shadow AND every
    # systems.*.adapter to mock - whatever the hotel has configured in
    # config/hotel.yaml. Passing provider/mode by hand (the previous version
    # of this function) left the PMS/email/messaging adapters as the hotel
    # configured them, so `make demo` on a property with a real adapter
    # already wired up could try to reach it. See factory/workflows/
    # build-repo.md section 5.
    try:
        settings = load_settings(demo=True)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    demo_db = sub_data_dir("demo") / "demo.db"
    if demo_db.exists():
        demo_db.unlink()
    store = Store(settings, path=demo_db)
    store_ext.ensure_schema(store)
    _seed_experiences(store)

    email = get_email(settings)
    messages = email.fetch_unread(limit=50)
    messaging = get_messaging(settings)
    chats = messaging.fetch_new(limit=50)
    if not messages and not chats:
        print("no fixtures found in fixtures/inbound/ - nothing to demo", file=sys.stderr)
        return 1

    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0}
    print(f"Front Desk AI demo - {len(messages)} email(s) + {len(chats)} chat message(s) "
         f"from fixtures/inbound/\n")
    print("Loop A - the inbox\n")
    for msg in messages:
        item, _ = process_message(settings, store, "email", email_to_dict(msg),
                                  channel="email", provider="mock")
        stats["processed"] += 1
        stats["drafted"] += 1
        if item.review_status == "needs_human":
            stats["needs_human"] += 1
        print(f"  email {msg.id}: \"{msg.subject}\" -> intent={item.intent} "
             f"lane={(item.draft or {}).get('lane', '-')} "
             f"confidence={item.confidence:.2f} status={item.review_status}")
    for msg in chats:
        item, _ = process_message(settings, store, "whatsapp", chat_to_dict(msg),
                                  channel="whatsapp", provider="mock")
        stats["processed"] += 1
        stats["drafted"] += 1
        if item.review_status == "needs_human":
            stats["needs_human"] += 1
        print(f"  whatsapp {msg.id}: \"{msg.text[:40]}\" -> intent={item.intent} "
             f"confidence={item.confidence:.2f} status={item.review_status}")

    print(f"\n{stats['needs_human']} of {stats['processed']} need a person to look first - "
         f"see knowledge/policies.md for what always does.")

    print("\nLoop B - the Greeter (confirmations + reminders)\n")
    b_stats = process_new_bookings(settings, store, now=DEMO_NOW)
    print(f"  {b_stats['bookings']} booking(s) landed (PMS/OTA fixtures in "
         f"fixtures/hotel/reservations.json)")
    print(f"  {b_stats['confirmations_queued']} confirmation(s) queued")
    print(f"  {b_stats['reminders_queued']} reminder(s) queued")
    print(f"  {b_stats['lapsed']} reminder slot(s) already lapsed and dropped")

    print("\nNothing was sent: mode is shadow, and demo never calls send() at all.")
    print("Next: `make review` to see every draft, or read workflows/10-front-desk.md.\n")

    print(f"DEMO OK - {summary_line(stats, settings.mode)}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
