# Workflow: shadow to live

Objective: decide, together with the hotel, whether Front Desk AI is ready to
send approved replies (and confirmations, and reminders) on its own instead
of only drafting them - and make the change safely if so.

This is the hotel's decision, never the agent's. Do not suggest it until the
checklist below is genuinely true, and when you do raise it, say plainly
what changes.

## Checklist

- [ ] `make doctor` is clean (no `FAIL` lines). `warn` on `mode` is expected
      until you flip it.
- [ ] `config/hotel.yaml` has the real property name, address and contact
      details, and every `knowledge/*.md` file (not the `.example.md`
      shipped copies) exists and is accurate - especially `policies.md`,
      which every prompt reads.
- [ ] `config/agent.yaml` has your property's real room types, restaurant
      hours and spa menu, not Hotel Aurora's.
- [ ] At least a few days of real `make run` passes (and, if you use it,
      `make run ARGS="--confirmations"`) have gone through the review queue,
      not just the demo fixtures.
- [ ] The edit rate in `make report` is one you are comfortable with for the
      intents you are about to trust - see `workflows/85-coach-weekly.md`.
- [ ] The hotel has decided on, and added, the AI-disclosure line to
      `knowledge/signature.md` (`docs/safety.md` has suggested wording and
      the EU AI Act Article 50 context).
- [ ] A real mailbox is connected (`systems.email.adapter: imap` or `gmail`)
      and, if used, a real messaging account - `make doctor` shows both
      healthy. Going live on the `mock` adapters would only ever touch the
      fixtures.
- [ ] If Specialist Booking is on, its catalogue in
      `fixtures/hotel/experiences.json` (or wherever you moved it) reflects
      real sessions and real capacity.

## Making the change

1. **Clear the shadow-era backlog first:**
   ```bash
   python3 tools/review.py stale
   ```
   Anything still sitting in `pending_review`, `needs_human`, `approved` or
   `edited` moves to `stale`. Approving something during shadow only ever
   recorded the decision - it was never sent - so this queue may be days or
   weeks old by the time you go live. Without this step, flipping to live
   would let all of it send at once, in a single pass, the instant the mode
   changes. A human can still revive one specific item afterwards
   (`stale -> pending_review`) if it genuinely still matters.
2. Edit `config/hotel.yaml`:
   ```yaml
   mode: live
   ```
3. `review.require_approval_for` still lists `send_email`, `send_message`
   and `pms_write` by default - it should. Going live means **approved
   drafts get sent and the booking behind them gets written**, not that
   Front Desk AI starts acting on unapproved items. There is no config that
   changes that.
4. Run `make doctor` again to confirm.
5. Run one real pass and manually watch a send go through:
   ```bash
   make run ARGS="--limit 1"
   python3 tools/review.py list
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
6. Tell the hotel exactly what just changed: an approved item now actually
   leaves the mailbox (and, for a booking, is written to the ledger and any
   configured PMS) the next time someone, or a scheduled job, runs
   `python3 tools/review.py send` - it is still never automatic before that
   approval, and everything except an approved item still waits for a
   person.

## Going back to shadow

```yaml
mode: shadow
```
in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env` for one run. Either
stops every outbound action - sends and booking writes alike - on the next
pass, mid-schedule, with no other change required.
