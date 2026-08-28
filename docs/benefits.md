# Measuring the benefit

## The promise

**Output.** Handles ~70-85% of routine guest email end-to-end; first reply
<5 min, 24/7. ~85% intent-classification accuracy live.

**ROI.** -78% Front-desk email workload (labor)

Those numbers come from the demo platform's roster and describe real
production use elsewhere in the family this repo was built from - they are
not a guarantee for your property. `make report` is how you find out what is
actually true for you.

## What to track

| Metric | Where it comes from | What it tells you |
|---|---|---|
| Volume by kind and status | `store.counts()` | how much mail/chat is landing, and how much of it is still waiting on a person |
| Auto-handled % | items `sent`/`auto_sent` with no edit, over everything terminal | the closest number to "handled end to end with no human touch" - honestly close to 0% while `autonomy: draft` is the default, and that is by design (see `docs/safety.md`) |
| Edit rate | edited vs. approved-unchanged, from `learnings` | the number `workflows/85-coach-weekly.md` exists to bring down - below 10% is the roster's own bar for "earned autonomy" |
| Time to first reply | guest message received -> draft ready for a human | the number behind "first reply <5 min" - bounded by `schedule.triage`, not by how fast a human happens to check the queue |
| Spend | `core.llm`'s usage logging | LLM calls, tokens, and cost - `0.00` is expected and correct on `mock`, `interactive` or `claude-code`; only `anthropic` bills per token |

Run it any time:

```bash
make report
python3 tools/report.py --json     # for a dashboard or a spreadsheet import
```

## Reading the auto-handled number honestly

This repo ships in `mode: shadow` with `autonomy: draft` - every reply, every
booking, every confirmation and every reminder waits for a human before
anything leaves the building. That means the auto-handled percentage will
read close to 0% until a hotel has watched the queue long enough to trust
specific intents and moved past `workflows/90-go-live.md`. A rising **edit
rate going down** and a **falling time to first reply** are the honest
leading indicators before that point - the roster's "70-85% handled end to
end" describes a property that has already gone live and tuned its
knowledge base, not day one on `mock` fixtures.

## The labor-saving case

The roster's "-78% front-desk email workload" is a labor claim: it assumes a
human is no longer reading and typing every routine reply from scratch, only
reviewing a draft (or, post go-live, only the exceptions). `make review`
timed against how long the same inbox used to take by hand is the simplest
way to see whether that holds for your property - there is no synthetic
number this repo can print that substitutes for that comparison.

## Caveats, plainly

- Numbers are only as good as `knowledge/`. A property that has not filled
  in `policies.md`, `property.md` and `faq.md` with real facts will see a
  higher edit rate and more `needs_human` items than the roster figures
  above - that is the system working correctly, not underperforming.
- `time_to_first_reply` measures detection-to-draft, not draft-to-send. A
  drafted, unreviewed queue does not help a guest; pair this metric with
  how often someone actually works `workflows/80-review.md`.
- `spend` only ever reflects the `anthropic` provider. Choosing
  `interactive` or `claude-code` to run on a subscription instead is a
  deliberate cost decision covered in `docs/safety.md` - `tools/report.py`
  will correctly show USD 0.00 in that case, which is not the same as "no
  cost".
