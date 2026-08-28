# knowledge/

This folder is the agent's memory of your property. It reads these files before
it answers anything, so the quality of what is in here is the quality of what
goes out.

## What to put here

| File | What it holds |
|---|---|
| `property.md` | The facts. Rooms, restaurant, spa, experiences, times, prices, policies. |
| `faq.md` | Questions guests actually ask, and the answers you actually give. |
| `policies.md` | The escalation list - what always goes to a person, verbatim enough to quote. Loaded into every prompt; see `prompts/triage.md`. |
| `room-descriptions.md` | Short, sellable copy for each room type, separate from the plain facts table in `property.md`. |
| `signature.md` | The sign-off on outgoing email and the AI-disclosure line (EU AI Act Article 50 - see `docs/safety.md`). |

Copy the `.example.md` files, rename them without `.example`, and fill them in:

```bash
cp knowledge/property.example.md         knowledge/property.md
cp knowledge/faq.example.md              knowledge/faq.md
cp knowledge/policies.example.md         knowledge/policies.md
cp knowledge/room-descriptions.example.md knowledge/room-descriptions.md
cp knowledge/signature.example.md        knowledge/signature.md
```

`make setup` does **not** do this for you - unlike `config/*.yaml`, these files
are meant to take real thought, not a blind copy.

`knowledge/*.md` is gitignored (the `.example.md` files are not), because your
property notes are yours.

## How to write it

**Write it the way you would brief a new receptionist.** Short sentences,
concrete facts, no marketing language. The agent will quote this material to
guests, so anything vague here becomes something vague in an email.

**Be specific about numbers and times.** "Check-in from 15:00" is usable.
"Check-in in the afternoon" is not.

**Say what you do NOT do.** "We have no parking; the nearest car park is X, about
EUR 15 a day" prevents a wrong answer far better than silence does.

**Keep prices dated.** "Breakfast EUR 18 per person (2026 rates)" tells the agent
and you when it is stale.

**One fact per line where you can.** It makes the agent's job easier and it makes
your job easier when something changes.

## Keeping it current

The agent is only as right as this folder. When a policy changes, change it here
first. A good habit: whenever you correct one of the agent's drafts in the review
queue, ask whether the correction belongs in `property.md`. If it does, the agent
stops making that mistake.

You can also ask your Claude Code session to do it:

> Read knowledge/property.md and the last ten items in the review queue. If any
> of my edits contradict what is in the file, tell me which line to change.

## `rules.md` - written by the Coach, not by you

`knowledge/rules.md` does not ship with the repo and has no `.example.md`.
`tools/coach.py apply` creates it the first time you accept a proposal, and
appends one line per accepted proposal after that. It is loaded into every
prompt alongside `property.md` and `faq.md`. See `workflows/85-coach-weekly.md`.
You can edit or delete lines in it by hand at any time - it is a plain file,
not a database.
