# Workflow: the front-desk loop (Loop A)

Objective: run one pass over the inbox - email and WhatsApp together - and
see what Front Desk AI did with each message.

## Inputs

- A configured `systems.email.adapter` and, if you use chat,
  `systems.messaging.adapter` (both `mock` by default - see
  `workflows/00-setup.md` step 5 to connect real ones).
- `config/agent.yaml`'s `confidence_threshold`, `rooms`, `restaurant` and
  `spa` blocks - the defaults (Hotel Aurora's own room types and menu) work
  for a dry run; replace them with your property's real ones before going
  live.

## Steps

1. **Run one pass.**
   ```bash
   make run
   make run ARGS="--limit 5"       # just the first five messages
   make run ARGS="--dry-run"       # compute everything, write nothing
   ```
   Every new message - email or WhatsApp - is triaged (`prompts/triage.md`)
   into an intent and a lane (`front_desk` or `specialist`), the relevant
   booking is previewed against your real room types, restaurant hours and
   spa menu (`tools/booking.py`, `tools/pricing.py` - no model call), then a
   reply is drafted (`prompts/draft.md`). See `docs/how-it-works.md` for the
   full flowchart.

2. **If `llm.provider` is `interactive`,** the run stops with exit code 3 and
   parks a prompt in `data/pending/`. Read `*.prompt.md`, write your answer as
   JSON to the matching `*.answer.json` exactly matching the schema shown, and
   run the same command again. Do this for triage and then again for draft,
   for each message.

3. **See what happened.**
   ```bash
   make review
   ```
   A routine question, booking or guest fact above `confidence_threshold` is
   `pending_review`. Anything that trips a guardrail in `knowledge/policies.md`
   - a complaint, a payment issue, a large group, a missing detail, or low
   confidence - is `needs_human`, on purpose (`docs/safety.md`).

4. **Work the queue.** `workflows/80-review.md` covers approve / edit /
   reject / send in full. Approving and sending a room, table or spa message
   is also the moment the real booking is written - see
   `docs/how-it-works.md` design decision 1.

5. **Keep it running.**
   ```bash
   make watch                       # loop on the configured interval
   ```
   Or schedule it - `scheduler/` has cron, launchd and systemd examples.
   `config/agent.yaml`'s `schedule.triage` documents the interval this repo
   was built around (every 5 minutes - guest mail is time-sensitive).

## Edge cases

- **No new mail or messages.** `make run` prints `0 items processed, 0
  drafted, 0 sent` and exits 0. Nothing to do.
- **A message the model cannot answer cleanly.** `core.llm` raises
  `LLMSchemaError` rather than accept a bad answer; the item is queued as
  `needs_human` with the error recorded, instead of guessing.
- **A restaurant table request lands on a closed day.** This is a normal
  reply offering the nearest other evening, not automatically a human
  matter - see `tools/engine.py:needs_human_for`.
- **A room or table request is oversized** (party size or room count over
  the thresholds in `config/agent.yaml`). Always escalates, even though the
  booking itself is technically valid.
- **A re-run sees the same message again.** `tools/engine.py` skips anything
  the store has already seen - see `core.store.Store.upsert_item`.
