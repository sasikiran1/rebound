# Resolve results (Nov 3, 2026)

Protocol: [resolve_protocol.md](resolve_protocol.md). Held-out cases frozen in `d63d0dd` before any model saw them;
prompts `resolve_interpret_v2.md` / `resolve_revise_v2.md` and decision code frozen at `46d4250` for every held-out
run below (later commits change display text and add an opt-in confirm mode that these runs do not use). Synthetic
fixtures, scripted customer turns, builder-written cases. 24 cases is a prototype check, not a reliability estimate.
Run records: `runs/resolve/` (not in git).

## Held-out outcomes (24 conversations, judged in SQL)

| Arm | Model | Correct | Unauthorized | Missed booking | Ideal question count | Model calls | Spend |
|---|---|---|---|---|---|---|---|
| **resolve** | Muse Spark 1.3 | **23** | **1** (H15) | 0 | 23 of 23 | 34 | $0.234 |
| one-shot | Muse Spark 1.3 | 15 | 2 (H15, H16) | 7 | (cannot ask) | 24 | $0.132 |
| form (correct entries by construction) | none | 24 | 0 | 0 | 22 of 23 | 0 | $0 |
| resolve | qwen3-14b, local | 16 | 2 (H17, H22) | 6 | 19 of 23 | 47 | $0 |
| one-shot | qwen3-14b, local | 10 | 0 | 14 | (cannot ask) | 24 | $0 |

"Correct" counts both correct bookings and correct refusals to book (4 cases must end with no booking).
The owner's gate was at least 22/24 and zero unauthorized bookings: **Muse resolve meets the first part (23/24) and
misses the second (1 unauthorized).** Locally neither part is met.

### By category (Muse)

| Category | resolve | one-shot |
|---|---|---|
| straightforward (4) | 4 | 4 |
| conditional alternatives (4) | 4 | 3 |
| negation (3) | 3 | 3 |
| references to history (4) | 3 | 0 (3 missed, 1 unauthorized) |
| corrections (3) | 3 | 1 (1 unauthorized, 1 missed) |
| renewed permission (3) | 3 | 1 |
| must stay unresolved (3) | 3 | 3 |

The gap between the two Muse arms (23 vs 15) sits where the design says it should: grounding in booking history,
asking before a trade-off, and applying a later correction. On plain, conditional and negated requests a single
call already does as well.

### The failures

- **H15, Muse resolve and one-shot (unauthorized).** "Keep the same time of day as my current booking, just move it
  to Friday." The model wrote "before 12:00" instead of the current start, 10:30, and 09:00 was booked. The quote
  check passed (the words were real); the meaning was coarsened. In confirm mode the customer would have been asked
  "Friday 9:00 AM, shall I book it?" before any write; this run did not use confirm mode.
- **H16, Muse one-shot (unauthorized).** The correction "Sorry, I meant Thursday morning" was not applied by the single
  call; the afternoon slot was booked.
- **H17, local (unauthorized).** "Friday" resolved to the 12th (a Thursday); the correction then dropped the date. A
  weekday-versus-date consistency check would catch this class; it was identified on held-out data, so it is *not*
  applied to these runs and is listed as a follow-up.
- **H22, local (unauthorized).** "The technician I had in September" matched two visits; the model picked one instead
  of marking it unclear. The deterministic ambiguity check only covers shared first names.
- Local misses concentrate on conditional alternatives (H05-H07), negation (H11), a negative reference (H14) and
  one permission case (H19).

## Two separate questions

1. **Did the final database effect satisfy the authorized request?** With Muse, in 23 of 24 cases; one booking (H15)
   was outside what the customer authorized. The form, given correct structured entries, satisfied all 24: the
   shared execution path (evaluator, store, worker) made no errors when the intent was right.
2. **Did conversational interpretation help?** Against one-shot extraction with the same model, yes: +8 cases, and
   the gain is in references, corrections and renewed permission. Against a capable form filled in correctly, no:
   the form is at least as correct. Effort is exploratory and builder-measured: the customer typed 401 words over
   the 24 conversations; the form needed 160 values set (kinds, dates, times, technicians, strengths, scopes).
   Whether 401 words of natural text are less work than 160 form values for real customers is not measured here.

## Development record (dev cases, not held out)

| Run | Model / prompt | Correct | Unauthorized |
|---|---|---|---|
| dev-r1 | qwen3-14b, v1 | 1/6 | 3 |
| dev-r2, dev-r3 | qwen3-14b, v2 (one corrective iteration, structural) | 4/6 | 0 |
| dev-r4 | qwen2.5-14b / gemma4-12b, v2 | 2/6 / 3/6 | 2 / 1 |
| muse-dev | Muse Spark 1.3, v2 | 6/6 | 0 |

## Recovery checks (live stack, `runs/resolve/faults/faults-r3`)

9 of 9 pass: an eligible slot at row 61 of 61 (first page 50) is found and booked; seven invalid-intent kinds are
rejected with no booking; a worker killed after claim, after recording its attempt, or after the POST returned leads
to exactly one change; a real lost acknowledgment (database link slowed during the write, worker timed out, commit
landed) reconciles with `replayed: true`; an approved slot taken before execution leads to a new question, not a
different booking; a correction does not drop the Friday rule; confirm mode books nothing before an explicit yes.
Run 1 found a validator crash on an invented technician id (fixed); run 2 was 8/8; run 3 added the confirm check.

## Demonstration (`runs/resolve/demo/demo-muse-1`, Muse, 2 model calls, $0.014)

Dev case D1: "after three this week, Dana if possible, check with me before next week." Real outage (5 s latency on
the database link): the worker's search times out and the request waits; meanwhile another customer takes Friday
15:30 with Dana. After recovery the worker finds no authorized slot and asks, offering two real open slots and quoting
the words they break. Scripted reply "Monday is okay, but not before four": version 2 widens the date range to Monday
and adds a 4 PM floor on Monday only; every other rule is unchanged. Confirm mode asks "Monday Nov 16 at 4:30 PM with
Alex, shall I book it?"; scripted yes (version 3). The worker process is killed right after its POST returns; the
commit is in SQL. A new worker reclaims the expired lease, resends with the same key, gets `replayed: true`. SQL: one
change, to the expected slot, authorized when applied.

## Spend

Muse for Resolve: 69 calls, **$0.443** (dev $0.063, held-out resolve $0.234, held-out one-shot $0.132, demo $0.014),
within the approved $2.55 / 141-call cap. Median call latency 4.4 s, 90th percentile 9.1 s. Cumulative project spend
recorded: $50.51.

## Limitations

- Synthetic fixtures and scripted replies written by the builder, who also wrote the expected outcomes and the form
  entries. No customer study; the effort comparison is exploratory.
- 24 held-out cases: one run per arm and model. No confidence intervals are claimed.
- No rule-parser baseline was built; whether a hand-written parser matches the model on these cases is untested.
- No notification channel: questions and confirmations are stored, not sent. A customer who left would not see them.
- The worker guarantee is the tested one (one worker per request, the injected crash points, FieldDesk's
  idempotency key), not general exactly-once execution.
- Interpretation errors that quote real words but change their meaning (H15) pass the validator; confirm mode is the
  intended guard and was demonstrated, not evaluated on the held-out set.
