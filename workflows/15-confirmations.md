# Workflow: confirmations and reminders (Loop B - the Greeter)

Objective: make sure a booking that lands with nobody having written in yet -
straight through the PMS, an OTA, the booking widget, or an accepted upsell -
still gets a personalised confirmation and the timed reminders that protect
show-up rates, in the guest's own language.

This is a separate loop from `workflows/10-front-desk.md`. It never calls a
model - see `tools/confirmations.py` and `docs/how-it-works.md` ("Loop B -
the Greeter").

## Inputs

- A configured `systems.pms.adapter` (`mock` by default, reading
  `fixtures/hotel/reservations.json`). A room/table/spa/experience booking
  Front Desk AI itself just finalised (`workflows/10-front-desk.md`) is
  watched too - only the reminder is scheduled for those, since the guest
  already received Loop A's own confirmation reply.
- `config/agent.yaml`'s `confirmations` block: `hold_for_review_over_eur`,
  `room_reminder`, `restaurant_reminder_enabled`,
  `ancillary_reminder_offsets`, `languages`.

## Steps

1. **Run one pass.**
   ```bash
   python3 tools/run.py --once --confirmations
   ```
   Every booking not seen before gets a confirmation queued (routine ones go
   to `pending_review`; anything over `hold_for_review_over_eur` goes to
   `needs_human` so a person double-checks a high-value booking before it
   goes out) and, unless it has already lapsed, its reminder(s).

2. **See what happened.**
   ```bash
   make review ARGS="--kind confirmation"
   make review ARGS="--kind reminder"
   ```
   Confirmations and reminders sit in the same queue as guest replies -
   `workflows/80-review.md` covers approve / edit / reject / send exactly the
   same way.

3. **Keep it running.**
   ```bash
   python3 tools/run.py --watch --confirmations
   ```
   Or schedule it separately from Loop A - `config/agent.yaml`'s
   `schedule.confirmations` documents the interval this repo was built
   around (every 15 minutes).

## Edge cases

- **A reminder slot has already passed** (the event itself is in the past,
  or the offset's own due time already lapsed). Dropped silently rather than
  sent late - see `tools/confirmations.py:plan_reminders`.
- **A restaurant reminder.** Off by default
  (`confirmations.restaurant_reminder_enabled: false`) because most
  restaurant systems send their own SMS reminder; turn it on only if yours
  does not.
- **The guest's language.** Follows the guest, not the passport - see
  `docs/how-it-works.md` design decision 8. A booking Front Desk AI itself
  created uses the language the guest actually wrote in; a PMS/OTA booking
  falls back to the guest's phone country code, then their country, then the
  hotel's default language.
- **A booking with no email and no phone on file.** Queued anyway on the
  `email` channel with an empty address - `make doctor` will not catch this;
  check `python3 tools/review.py show <id>` before approving one.
