# Workflow: Specialist Booking AIs ("The Specialists")

Objective: turn on and run the sub-agent that books the experiences living
outside the PMS - wine tastings, sunrise yoga, an after-work social, a
ticketed rooftop party - each with live capacity, not guessed availability.

Off by default: Front Desk AI fully handles rooms, tables and spa on its
own, and most properties do not sell ticketed experiences. Turn this on only
once your experience catalogue is real - see `docs/sub-agents.md`.

## Turning it on

1. Write your real sessions into `fixtures/hotel/experiences.json` (or your
   own file - see `tools/store_ext.py:seed_experience_sessions`), one entry
   per session: `slug`, `title`, `schedule_label`, `next_date`, `start_time`,
   `price`, `capacity`, `venue`, `host`. `price` is a plain number in
   `hotel.currency` - never hardcoded to EUR. This seeds the
   `experience_sessions` table the first time `tools/run.py` runs; an
   existing slug is left untouched so live booked counts are never reset.
2. In `config/agent.yaml`:
   ```yaml
   subagents:
     specialist_booking:
       enabled: true
   ```
3. Add each experience to `knowledge/property.md` under "Bookable
   experiences" so the triage step knows they exist and can route a guest's
   question to this lane instead of guessing.
4. `make doctor` - "specialist booking" should now say `ok` with the
   catalogue present.

## Running it

There is nothing extra to run - it shares Loop A entirely
(`workflows/10-front-desk.md`). A message about a listed experience gets
`lane: specialist` from the triage step; `tools/booking.py:preview_experience`
checks live spots against `experience_sessions.booked` and rejects an
oversell with the nearest alternative instead of double-booking; approving
and sending the guest's reply is what actually books the session
(`tools/booking.py:finalize_booking`, kind `experience`).

## Edge cases

- **Off, and a guest asks about an experience anyway.** The message routes
  back to `front_desk`, the intent becomes `question`, and a note is added
  to `missing_info` so it always reaches a human - see
  `tools/engine.py:apply_lane_gate`.
- **A session is full.** `preview_experience` returns an error naming how
  many spots are left; the draft step offers those or the next session,
  never invents extra capacity.
- **A message touches both a room and an experience.** Triage commits to one
  `lane` up front; a genuinely mixed message comes back with `missing_info`
  asking which one first, rather than silently crediting the wrong agent -
  see `docs/how-it-works.md` design decision 2.
