# Workflow: first-run setup

Objective: get Front Desk AI from a fresh clone to a working demo, then to
real config, in one sitting.

## Steps

1. **Install and check.**
   ```bash
   make setup
   make doctor
   ```
   `make setup` creates the virtualenv, installs `requirements.txt`, and
   copies `.env.example` -> `.env` and every `config/*.example.yaml` ->
   `config/*.yaml` (only if those files do not exist yet - it never overwrites
   your own copies). `make doctor` will show a `FAIL` on "hotel identity"
   right after setup - that is expected, it means the property name is still
   the shipped placeholder. Everything else should be `ok` or `warn`.

2. **Run the demo.** No credentials needed.
   ```bash
   make demo
   ```
   Expect to see 10 sample emails and 3 WhatsApp messages triaged and
   drafted (Loop A), then 3 sample PMS bookings confirmed and reminded
   (Loop B), and the line
   `DEMO OK - 13 items processed, 13 drafted, 0 sent (shadow)`. If you do not
   see that, stop and read `workflows/99-troubleshooting.md` before going
   further.

3. **Fill in the property.** Edit `config/hotel.yaml` (name, address, contact,
   languages), then `config/agent.yaml` (your actual room types, restaurant
   hours, spa menu - see the comments in `config/agent.example.yaml`). Then:
   ```bash
   cp knowledge/property.example.md          knowledge/property.md
   cp knowledge/faq.example.md               knowledge/faq.md
   cp knowledge/policies.example.md          knowledge/policies.md
   cp knowledge/room-descriptions.example.md knowledge/room-descriptions.md
   cp knowledge/signature.example.md         knowledge/signature.md
   cp knowledge/disclosure.example.md        knowledge/disclosure.md
   ```
   Replace the Hotel Aurora content with your own facts. See
   `knowledge/README.md` for how to write it well - `policies.md` in
   particular is loaded into every prompt, so getting the escalation list
   right matters more than any other file here. `signature.md` is appended
   to every outbound email (`Email.with_signature()`); `disclosure.md` is
   the one-sentence AI-disclosure line appended to every WhatsApp/chat send
   (`Messaging.with_disclosure()`) - both carry the EU AI Act Article 50
   line, see `docs/safety.md`.

4. **Pick how the agent thinks.** `config/hotel.yaml`'s `llm.provider` starts
   as `interactive` - it asks you, in this Claude Code session, instead of
   calling a model. That costs nothing extra and is the best way to see how
   Front Desk AI reasons. `docs/how-it-works.md` and `docs/safety.md` explain
   the other three providers (`mock`, `claude-code`, `anthropic`) and when to
   move to one of them.

5. **Connect a real mailbox and, if you use it, WhatsApp (optional for now).**
   `systems.email.adapter` and `systems.messaging.adapter` in
   `config/hotel.yaml` start as `mock`, which only ever sees the bundled
   fixtures. `docs/integrations.md` covers `imap`/`gmail` and
   `unipile`/`webhook`. Run `make doctor` after changing either.

6. **Decide on the two sub-agents and the coach.** `config/agent.yaml`'s
   `subagents` block: `specialist_booking` starts **off** (see
   `workflows/20-specialist-booking.md`), `whatsapp` and `coach` start **on**
   (there is nothing extra to configure for either until you connect a real
   messaging account or accumulate a week of edits).

7. **Re-check.**
   ```bash
   make doctor
   ```
   Once the property name is real and `knowledge/property.md` exists, the
   "hotel identity" and "knowledge" lines turn green. Move on to
   `workflows/10-front-desk.md` to run the loop for real.
