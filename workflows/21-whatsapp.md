# Workflow: WhatsApp / Live-Chat AI ("The Messenger")

Objective: turn on WhatsApp so guests get the same instant, conversational
handling as email, on the channel they actually use.

Same brain as Front Desk AI, not a separate agent: there is no
`tools/whatsapp.py` and no separate loop. `item.payload.channel` carries
`whatsapp`, and the draft prompt's own tone rule shortens the reply and
drops the subject line - see `docs/how-it-works.md`.

## Turning it on

1. Connect a real messaging adapter - `docs/integrations.md#messaging`
   covers `unipile` (your own WhatsApp number) and `webhook` (POST to
   Zapier/Make/n8n). `systems.messaging.adapter` starts as `mock`, which only
   ever sees `fixtures/inbound/messages.json`.
2. `config/agent.yaml`'s `subagents.whatsapp.enabled` is already `true` by
   default - there is nothing else to flip. It exists so you can turn
   WhatsApp off entirely (`false`) without touching `systems.messaging`.
3. `make doctor` - "messaging adapter" should show `ok` once connected.

## Running it

Also nothing extra to run - `tools/run.py` fetches
`core.adapters.get_messaging()` in the same pass as email
(`workflows/10-front-desk.md`). Everything else - triage, booking preview,
draft, review, send - is identical.

## What always escalates on this channel

Per the roster promise for this sub-agent: **payment disputes and
complaints always escalate**, same as email - see
`knowledge/policies.md` and `tools/engine.py:needs_human_for`. There is no
separate WhatsApp-only guardrail list; it is the same one, on a faster
channel.

## Edge cases

- **A very short, informal message.** The draft prompt is told explicitly to
  keep WhatsApp replies to a couple of sentences with no subject line - a
  reply here should never read like a formal letter.
- **A phone number instead of a name.** `chat_to_dict` in `tools/engine.py`
  falls back to the number when no name is on the message; the guest's real
  name from a PMS lookup is a good place to extend this if you connect a
  built PMS adapter.
- **`systems.messaging.adapter: mock`.** `make demo` and `make test` only
  ever see the 3 fixtures in `fixtures/inbound/messages.json` - add more
  there to try other conversations before connecting a real account.
