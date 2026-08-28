#!/usr/bin/env python3
"""tools/review.py - work the review queue: list / show / approve / edit / reject / send.

    python3 tools/review.py list [--status pending_review] [--kind message]
    python3 tools/review.py show <id>
    python3 tools/review.py approve <id> [--note "..."]
    python3 tools/review.py edit <id> --body-file draft.txt [--subject "..."] [--note "..."]
    python3 tools/review.py reject <id> --reason "wrong tone"
    python3 tools/review.py retry <id>          # re-queue a failed send
    python3 tools/review.py send                # send everything approved/edited

The queue mixes three kinds of item: ``message`` (a guest email or chat reply,
from Loop A), and ``confirmation`` / ``reminder`` (from Loop B - see
tools/confirmations.py). All three share one FSM (core/store.py) and one
send step here.

Only this tool writes `approved` / `edited` / `rejected` (core/review.py).
Only `send` writes `sending` / `sent`. For a ``message`` item, ``send`` also
calls ``tools/booking.py:finalize_booking`` first - the real booking write
happens at the same moment the confirmation goes out, and only for an item a
human approved (see docs/how-it-works.md design decision 1). Nothing here
bypasses `mode: shadow` - see docs/safety.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_messaging  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.review import (WriteBlocked, approve, edit, list_queue, reject, retry,  # noqa: E402
                         show, stale_backlog)
from core.store import Store, StoreError  # noqa: E402

from booking import finalize_booking  # noqa: E402


def _print_item_line(item) -> None:
    payload = item.payload or {}
    draft = item.draft or {}
    # Prefer an email subject, then the GUEST'S OWN message (payload.body) -
    # every WhatsApp item has no subject, and showing the AI's own drafted
    # reply there instead of what the guest actually wrote (the original bug)
    # is exactly backwards for a human triaging the queue. A confirmation/
    # reminder item has no guest message of its own (Loop B generated it),
    # so it falls back to the draft, which is the right thing there.
    label = payload.get("subject") or payload.get("body") or draft.get("subject") or \
        draft.get("body", "")
    # `item.is_sample` is set by core (core/store.py) for anything read
    # through a mock adapter outside `make demo` - see docs/integrations.md
    # "Sample data is labelled". A human working the real queue must never
    # mistake a shipped fixture for a real guest.
    marker = "  [SAMPLE DATA]" if item.is_sample else ""
    print(f"  {item.id}  {item.review_status:<14} {item.kind:<12} {item.intent or '-':<16} "
         f"{label[:45]}{marker}")


def cmd_list(store, args) -> int:
    items = list_queue(store, status=args.status, kind=args.kind, limit=args.limit)
    if not items:
        print("Nothing is waiting for you.")
        return 0
    print(f"{len(items)} item(s) waiting:\n")
    for item in items:
        _print_item_line(item)
    print("\nRun `python3 tools/review.py show <id>` for the full draft.")
    return 0


def cmd_show(store, args) -> int:
    try:
        detail = show(store, args.id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if (detail["item"].get("payload") or {}).get("_sample"):
        print("[SAMPLE DATA] this item was read through a mock adapter, not your "
             "property - see docs/integrations.md.\n")
    print(json.dumps(detail, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_approve(store, args) -> int:
    item = approve(store, args.id, note=args.note or "")
    print(f"approved {item.id} - now in the send queue")
    return 0


def cmd_edit(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    body = Path(args.body_file).read_text(encoding="utf-8")
    new_draft = dict(item.draft or {})
    new_draft["body"] = body
    if args.subject:
        new_draft["subject"] = args.subject
    edit(store, args.id, new_draft, note=args.note or "")
    print(f"edited {item.id} - now in the send queue")
    return 0


def cmd_reject(store, args) -> int:
    item = reject(store, args.id, reason=args.reason or "")
    print(f"rejected {item.id}")
    return 0


def cmd_retry(store, args) -> int:
    item = retry(store, args.id)
    print(f"queued {item.id} for another send attempt")
    return 0


def _channel_and_to(item) -> tuple[str, str]:
    payload = item.payload or {}
    draft = item.draft or {}
    channel = draft.get("channel") or payload.get("channel") or "email"
    if channel == "whatsapp":
        to = payload.get("chat_id") or payload.get("from_number") or payload.get("to") or ""
    else:
        to = payload.get("from") or payload.get("from_email") or payload.get("to") or ""
    return channel, to


def cmd_send(store, settings, args) -> int:
    claimed = store.claim_for_send(limit=args.limit)
    if not claimed:
        print("Nothing approved or edited is waiting to send.")
        return 0
    email = get_email(settings)
    messaging = get_messaging(settings)
    sent, failed = 0, 0
    for item in claimed:
        draft = item.draft or {}
        payload = item.payload or {}

        if item.kind == "message":
            try:
                finalize_booking(settings, store, item)
            except (WriteBlocked, AdapterError) as exc:
                store.mark_send_failed(item.id, f"could not finalize booking: {exc}")
                print(f"blocked {item.id} (approval kept): {exc}")
                failed += 1
                continue

        channel, to = _channel_and_to(item)
        try:
            if channel == "whatsapp":
                result = messaging.send(to, draft.get("body", ""), item=item)
            else:
                result = email.send(to, draft.get("subject", ""), draft.get("body", ""),
                                    reply_to_message_id=payload.get("message_id_header"),
                                    item=item)
        except WriteBlocked as exc:
            # Not a failure: the mode blocked it. The approval stands for go-live.
            store.transition(item.id, "approved", "agent", {"blocked": str(exc)[:200]})
            print(f"blocked {item.id}: {exc}")
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - record and move on, never crash the batch
            store.mark_send_failed(item.id, str(exc))
            print(f"failed {item.id}: {exc}")
            failed += 1
            continue
        store.mark_sent(item.id, result.get("message_id"))
        print(f"sent {item.id} ({item.kind} via {channel})")
        sent += 1
    print(f"\n{sent} sent, {failed} failed.")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="what is waiting for a human")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--kind", default=None)
    p_list.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show", help="full detail for one item")
    p_show.add_argument("id")

    p_approve = sub.add_parser("approve", help="approve the draft unchanged")
    p_approve.add_argument("id")
    p_approve.add_argument("--note", default="")

    p_edit = sub.add_parser("edit", help="rewrite the draft, then queue it")
    p_edit.add_argument("id")
    p_edit.add_argument("--body-file", required=True)
    p_edit.add_argument("--subject", default=None)
    p_edit.add_argument("--note", default="")

    p_reject = sub.add_parser("reject", help="discard the draft")
    p_reject.add_argument("id")
    p_reject.add_argument("--reason", default="")

    p_retry = sub.add_parser("retry", help="re-queue a failed send")
    p_retry.add_argument("id")

    p_send = sub.add_parser("send", help="send everything approved or edited")
    p_send.add_argument("--limit", type=int, default=20)

    sub.add_parser("stale", help="go-live step: mark every un-sent review row "
                                 "(pending_review / needs_human / approved / edited) as "
                                 "stale - the shadow-era queue was never sent and is out "
                                 "of date. Run this once, right before flipping mode: live.")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "show":
            return cmd_show(store, args)
        if args.command == "approve":
            return cmd_approve(store, args)
        if args.command == "edit":
            return cmd_edit(store, args)
        if args.command == "reject":
            return cmd_reject(store, args)
        if args.command == "retry":
            return cmd_retry(store, args)
        if args.command == "send":
            return cmd_send(store, settings, args)
        if args.command == "stale":
            moved = stale_backlog(store)
            print(f"marked {len(moved)} item(s) stale. Nothing from before go-live "
                 f"will be sent.")
            return 0
        parser.error(f"unknown command {args.command}")
        return 2
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    except (WriteBlocked, AdapterError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
