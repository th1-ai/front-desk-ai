# Workflow: Email Optimizer / Coach AI - weekly review

Objective: turn a week of human corrections into concrete improvements,
without ever letting the coach touch a guest or change behaviour on its own.

Run this weekly (`config/agent.yaml`'s `schedule.coach`, Monday 03:00 by
default). It reads `learnings` (every edit and reject from
`workflows/80-review.md`) and resolved `escalations` missing a suggestion -
see `docs/how-it-works.md` and `tools/coach.py`.

## Steps

1. **Analyze the week.**
   ```bash
   python3 tools/coach.py analyze
   ```
   Corrections are grouped by the intent or kind they happened on
   (`tools/coach.py:cluster_learnings`); a group at or above
   `coach.min_cluster_size` (default 2) is a real pattern and gets one model
   call to turn it into a concrete suggestion - a proposal, `status:
   pending`. Every resolved escalation missing an `improvement_suggestion`
   gets one too, written straight onto the escalation record.

2. **Review the proposals.**
   ```bash
   python3 tools/coach.py list
   python3 tools/coach.py show <id>
   ```
   Each one names a pattern, how many corrections it came from, a
   before/after example, and a suggested fix pointing at a specific file -
   usually a line to add to `knowledge/policies.md`, `knowledge/property.md`,
   or `knowledge/rules.md`.

3. **Decide, one at a time.**
   ```bash
   python3 tools/coach.py accept <id> [--note "..."]
   python3 tools/coach.py reject <id> [--note "why not"]
   ```
   A rejected proposal changes nothing and is not retried automatically.

4. **Apply what was accepted.**
   ```bash
   python3 tools/coach.py apply
   ```
   Writes one line per accepted, not-yet-applied proposal to
   `knowledge/rules.md` (created on first use - see `knowledge/README.md`)
   and marks each `applied`. `knowledge/rules.md` is loaded into every
   prompt from the next run on, alongside `property.md` and `policies.md`.
   You can edit or delete a line in it by hand at any time.

5. **Watch the trend.**
   ```bash
   make report
   ```
   The edit rate this prints is the number this whole loop exists to bring
   down - the roster's promise is that an agent's edit rate should fall
   below 10% as it earns full autonomy.

## Rules

- The coach never talks to a guest and never changes a prompt or a
  knowledge file on its own - only `apply`, after a human `accept`, does
  that, and only for the one proposal accepted.
- A proposal below `coach.min_cluster_size` stays a `learnings` row, not
  noise thrown away - it counts again next week if the pattern repeats.
- If a suggestion is vague or wrong, `reject` it and, if you can, tighten
  `prompts/coach-suggestion.md` rather than trying to fix it by hand in
  `knowledge/rules.md` - the next cluster will hit the same prompt.
