#!/usr/bin/env python3
"""tools/report.py - what the agent did, and what it cost.

    make report
    python3 tools/report.py
    python3 tools/report.py --json

Reads data/agent.db - nothing here calls a model or an adapter. Five numbers,
each tied to a roster claim (see README.md section 2 and docs/benefits.md):

``volumes``           items by kind and by review_status right now.
``auto-handled %``    of everything that has reached a terminal state, the
                       share sent with no human edit at all (``sent`` /
                       ``auto_sent`` with no ``edited`` event in its history).
``edit %``             of everything a human approved or edited, how often
                       they had to rewrite it rather than approve as-is - the
                       number workflows/85-coach-weekly.md exists to drive down.
``time-to-first-reply`` average minutes from a guest message arriving
                       (``payload.received_at``) to a draft being ready for a
                       human (the ``pending_review``/``needs_human`` event) -
                       the number behind the roster's "first reply <5 min".
``spend``              LLM calls, tokens and cost, from ``core.llm``'s usage
                       logging (``core.store.usage_totals``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, load_settings  # noqa: E402
from core.store import Store, StoreError, TERMINAL  # noqa: E402


def volumes(store: Store) -> dict:
    by_status = store.counts()
    rows = store.db.execute("SELECT kind, COUNT(*) AS n FROM items GROUP BY kind").fetchall()
    by_kind = {r["kind"]: r["n"] for r in rows}
    return {"by_kind": by_kind, "by_status": by_status, "total": sum(by_kind.values())}


def auto_handled(store: Store) -> dict:
    counts = store.counts()
    total_terminal = sum(counts.get(s, 0) for s in TERMINAL)
    auto = counts.get("auto_sent", 0)
    rate = (auto / total_terminal) if total_terminal else 0.0
    return {"auto_sent": auto, "terminal": total_terminal, "rate": rate}


def edit_stats(store: Store) -> dict:
    rows = store.db.execute(
        "SELECT item_id, action FROM events WHERE action IN "
        "('status:edited', 'status:approved')").fetchall()
    edited = {r["item_id"] for r in rows if r["action"] == "status:edited"}
    approved = {r["item_id"] for r in rows if r["action"] == "status:approved"} - edited
    total = len(edited) + len(approved)
    rate = (len(edited) / total) if total else 0.0
    return {"edited": len(edited), "approved_unchanged": len(approved), "rate": rate}


def time_to_first_reply_minutes(store: Store) -> dict:
    rows = store.db.execute(
        "SELECT id, payload_json, created_at FROM items WHERE kind='message'").fetchall()
    deltas: list[float] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        received = payload.get("received_at") or row["created_at"]
        events = store.db.execute(
            "SELECT ts FROM events WHERE item_id=? AND action IN "
            "('status:pending_review', 'status:needs_human') ORDER BY ts ASC LIMIT 1",
            (row["id"],)).fetchone()
        if events is None:
            continue
        try:
            start = datetime.fromisoformat(str(received)[:19])
            end = datetime.fromisoformat(str(events["ts"])[:19])
        except ValueError:
            continue
        deltas.append(max(0.0, (end - start).total_seconds() / 60.0))
    avg = (sum(deltas) / len(deltas)) if deltas else 0.0
    return {"n": len(deltas), "avg_minutes": round(avg, 1)}


def spend(store: Store, since: str | None = None) -> dict:
    return store.usage_totals(since=since)


def build_report(store: Store, since: str | None = None) -> dict:
    return {
        "volumes": volumes(store), "auto_handled": auto_handled(store),
        "edits": edit_stats(store), "time_to_first_reply": time_to_first_reply_minutes(store),
        "spend": spend(store, since=since),
    }


def print_report(report: dict) -> None:
    v = report["volumes"]
    print("Front Desk AI - report\n")
    print(f"Items: {v['total']} total")
    if v["by_kind"]:
        print("  by kind:   " + ", ".join(f"{k}={n}" for k, n in sorted(v["by_kind"].items())))
    if v["by_status"]:
        print("  by status: " + ", ".join(f"{k}={n}" for k, n in sorted(v["by_status"].items())))

    a = report["auto_handled"]
    print(f"\nAuto-handled: {a['auto_sent']}/{a['terminal']} terminal item(s) "
         f"({a['rate']*100:.0f}%) needed no human touch at all.")

    e = report["edits"]
    print(f"Edit rate: {e['edited']}/{e['edited'] + e['approved_unchanged']} approved "
         f"draft(s) needed a rewrite ({e['rate']*100:.0f}%). "
         f"See workflows/85-coach-weekly.md to bring this down.")

    t = report["time_to_first_reply"]
    if t["n"]:
        print(f"Time to first reply: {t['avg_minutes']} minute(s) average, over {t['n']} "
             f"guest message(s). Roster target: under 5.")
        if t["avg_minutes"] > 1440:
            # A number this large is almost always a clock artifact - a fixture
            # or an imported message whose received_at is not real wall-clock
            # time - not a real 24h+ first reply. See SIMULATION.md finding 15
            # and docs/benefits.md for the caveats worth knowing before you
            # quote this number to anyone.
            print("  (this looks unrealistically large - it usually means "
                 "received_at on one or more messages is not real wall-clock "
                 "time, e.g. a fixture or an older imported message. See "
                 "docs/benefits.md before quoting this number.)")
    else:
        print("Time to first reply: no completed messages yet.")

    s = report["spend"]
    print(f"\nSpend: {s['calls']} LLM call(s), {s['input_tokens']} input + "
         f"{s['output_tokens']} output token(s), USD {s['cost_usd']:.4f}.")
    if s["calls"] and s["cost_usd"] == 0.0:
        print("  (0.00 is expected on provider=mock, interactive or claude-code - only "
         "the anthropic provider bills per token.)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--since", default=None, help="ISO timestamp - only spend since then")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        report = build_report(store, since=args.since)
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
