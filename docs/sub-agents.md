# Sub-agents in this repo

Front Desk AI folds in two sub-agents and one coach layer. All three share
this repo's `core/`, `data/agent.db` and review queue - there is nothing
extra to install. Each is off or on independently; see
`config/agent.yaml`'s `subagents` block.

## Specialist Booking AIs - "The Specialists"

**Does.** Handles the bookable experiences that live outside the PMS - wine
tastings, sunrise yoga classes, after-work events, ticketed parties like the
rooftop full-moon night. Checks live availability for each session, books
the guest in, takes note of the occasion (birthdays, group sizes), and
confirms in the guest's own language. Each experience line gets its own
specialist sharing one triage brain, so a wine question and a yoga question
are each answered by an expert.

**Won't.** It never books rooms, restaurant tables, or spa treatments -
those are PMS bookings and belong to the Front Desk AI. It stays in its lane
per experience, routes multi-topic messages back through triage, and hands
anything high-stakes or below 80% confidence to a human.

**Why.** Hotels with real event and experience revenue (tastings, classes,
parties) lose those bookings when enquiries queue behind room email. Giving
each revenue line its own specialist means instant expert answers - while
the Front Desk AI keeps the core stay.

**Output.** Experience enquiries answered and booked live, with every
booking and note visible in the AI action log. High-confidence routing
across 6 intent categories.

**Off by default.** Most properties do not sell ticketed experiences, and
Front Desk AI is fully useful without this - rooms, tables and spa all work
regardless. Turn it on once your experience catalogue is real; see
`workflows/20-specialist-booking.md`.

## WhatsApp / Live-Chat AI - "The Messenger"

**Does.** Same brain as Front Desk AI but on WhatsApp and website chat:
instant, conversational, in any language, with booking and concierge
actions inline.

**Won't.** Escalates payment disputes and complaints.

**Why.** Guests increasingly expect chat, not email; instant beats inbox.

**Output.** <30-sec response on the channel guests actually use.

**On by default**, because it costs nothing to leave on - it only does
anything once `systems.messaging.adapter` is a real one; see
`workflows/21-whatsapp.md`.

## Email Optimizer / Coach AI

**Does.** The coach class. Each week it reads every guest reply a human
edited, rejected, or thumbed-down, clusters the corrections into patterns,
applies the safe knowledge-base fixes itself, and proposes the rest. A
sibling captures every human edit as a training pair, so the whole roster
keeps getting sharper. A live quality board tracks the numbers that matter -
replies sent unchanged, edit severity, hand-off rate - so you watch each
agent earn its autonomy week by week.

**Won't.** Doesn't talk to guests. Holds the higher-judgement changes for a
human nod; applies the clear-cut ones itself.

**Why.** This is what makes the whole roster get better over time instead of
plateauing. The difference between a bot and a system that learns from your
staff.

**Output.** Drives the human-edit rate down week over week; agents graduate
to full autonomy as their edit rate falls below 10%.

**On by default** (analysis only - it never touches a guest, so leaving it
on is safe from day one); see `workflows/85-coach-weekly.md`.
