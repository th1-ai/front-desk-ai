# Workflow: working the review queue

Objective: turn a queued item - a guest reply, a booking confirmation, or a
reminder - into a decision (approve, edit, or reject) and, once approved,
actually send it.

Nothing reaches a guest without going through this. `mode: shadow` blocks
every send except an item you have approved or edited; see `docs/safety.md`
for the full guard.

## Steps

1. **See what is waiting.**
   ```bash
   make review
   make review ARGS="--kind message"        # guest replies only
   make review ARGS="--kind confirmation"    # Loop B confirmations
   make review ARGS="--status needs_human"
   ```
   Each line shows the item id, its status, its kind, the intent Front Desk
   AI gave it, and a short label.

2. **Read one in full.**
   ```bash
   python3 tools/review.py show <id>
   ```
   This prints the original message, the triage result, the booking outcome
   that was previewed, the draft, and the full event history for that item.
   Summarise it for the hotel in plain language - who wrote in, what they
   need, what was drafted, how confident it was - do not paste the raw JSON
   at them.

3. **Decide.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py edit <id> --body-file my-version.txt [--subject "New subject"]
   python3 tools/review.py reject <id> --reason "wrong tone"
   ```
   `edit` records the before/after pair as a `learnings` row - that is what
   `workflows/85-coach-weekly.md` reads to spot patterns.

4. **Send what was approved.**
   ```bash
   python3 tools/review.py send
   ```
   For a `message` item, this **also finalises the booking** -
   `tools/booking.py:finalize_booking` writes the real room/table/spa/
   experience row (and the mandatory PMS note, and any staff forward) at the
   same moment the confirmation goes out, and only for an item you approved
   or edited - and only when the original booking preview actually
   succeeded; a request the engine already rejected (closed day, no
   availability) can never become a "confirmed" row. See
   `docs/how-it-works.md` design decision 1. For a `confirmation` or
   `reminder` item there is nothing to book - it just sends. In
   `mode: shadow`, `send` refuses **every** item, including one you just
   approved - see `core/review.py`. Your approve/edit/reject decisions are
   still recorded; they are simply not acted on until `mode: live`.

5. **A failed send.** `send` marks the item `failed` with the error attached.
   ```bash
   python3 tools/review.py retry <id>
   ```
   re-queues it for another attempt once you have fixed the cause (usually a
   mailbox or messaging credential - `make doctor` will say which).

## Rules

- Only `tools/review.py` writes `approved` / `edited` / `rejected`.
- Anything `needs_human` earned that status for a reason written into
  `knowledge/policies.md` or `config/agent.yaml` - read it, do not
  rubber-stamp it.
- Confirm with the hotel before sending anything, even an approved item, the
  first few times. `workflows/90-go-live.md` covers when to stop doing that.
