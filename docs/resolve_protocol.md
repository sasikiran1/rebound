# Resolve evaluation protocol

Rebound Resolve is a scheduling recovery assistant. When an outage or changed availability makes a customer's
request impossible, it interprets what the customer actually allows, asks before any trade-off they did not
authorize, keeps their correction, and completes the approved booking after recovery. This file fixes how that is
evaluated. Results are in [resolve_results.md](resolve_results.md).

## What is claimed, and what is not

Claimed only as measured: final database effects on 24 synthetic held-out conversations; unauthorized bookings;
clarification behavior; model calls, latency and spend; recovery behavior under the specific injected faults below.

Not claimed: production reliability, general exactly-once execution, a user study, recovered revenue, or that the
model is better than code at matching slots. A deterministic evaluator does all matching for every arm.

## Cases (frozen)

`data/resolve/heldout.yaml`: 24 conversations written by hand, committed in `d63d0dd` before any model saw
them (sha256 `7cbb7498...`). Categories: straightforward 4, conditional alternatives 4, negation 3, references to
booking history 4, corrections 3, unavailable first choice needing renewed permission 3, must remain unresolved 3.
Each case lists its own fixture world (technicians, history, current appointment, open slots), the slots another
customer takes while the request waits, the scripted customer turns, and, enumerated slot by slot from the
customer's words, the slots the customer has authorized after each turn and the correct final outcomes. These sets
do not use the rule vocabulary or any model output. `dev.yaml` (6 cases) is the only material used for prompt
development. Customer replies are scripted: one per question; when they run out the customer answers "I don't have
anything to add to what I already said."

## Arms (same fixtures, evaluator, store, worker and SQL oracle)

| arm | input | model |
|---|---|---|
| resolve | conversation + grounded facts (history, roster, calendar); validator; one repair call; questions and corrections | yes |
| oneshot | one call over the unprompted messages; no booking history; no repair; cannot ask | yes |
| form | capable conditional form: the structured entries written in each case file (rules with strength, scoped limits, two alternatives, roster and history picks, full edits per turn); `null` where a careful user could not fill it | no |

The form is not handicapped: it is given correct entries by construction, so its correctness measures the shared
execution path. It answers "given a correct structured intent, does the rest work?". The comparison with resolve
answers "how much correctness is lost or kept when the intent comes from words instead", and the exploratory effort
counts (customer words typed vs. form values set) are measured by me on my own cases, not a user study.

## Measures

- **Outcome** (oracle, `resolve/eval/oracle.py`): `correct`, `correct_no_booking`, `unauthorized`, `wrong_slot`,
  `missed_booking`, `duplicate_effect`. Read through the read-only role; turn times are read from the database clock
  by the harness. A booking is unauthorized if its slot was not in the authorized set for the number of customer
  turns delivered before it was applied.
- **Questions**: count against the case's ideal count (`null` = not scored).
- **Cost and latency**: model calls, tokens, USD at the provider prices in `harness/provider.py`, per-case wall time.
- **Effort (exploratory)**: customer words; form values set.

Engineering gate set before the run: at least 22/24 correct outcomes and zero unauthorized bookings.
It is a target, not a reliability estimate; misses are reported as they are.

## Models

- Local development and the local held-out run: `qwen3-14b-16k` (Ollama, temperature 0, seed 41, 16k context,
  thinking off), prompts `prompts/resolve_interpret_v2.md` and `prompts/resolve_revise_v2.md`.
- Paid run (only under a spending cap set in advance): Muse Spark through the Meta API with the same prompts and
  code, run once, every call retained. `--max-calls` and `--max-cost` stop the run before a call that would exceed
  the cap (the last call's own cost can overshoot by one call).

## Recovery checks (`resolve/eval/faults.py`, live stack)

Paging past the first 50 search results; seven invalid-intent kinds (reversed range, impossible date, outside the
horizon, invented technician, unnamed technician, unquoted words, impossible time window); worker killed after
claim, after recording the attempt and after the POST returned; a lost acknowledgment (Toxiproxy delays the
database link during the write so the worker times out while the commit lands); renewed permission after an approved
slot is taken; a correction that must not drop an unrelated constraint. Model outputs in these checks are canned, so
they test the deterministic path, not model luck.
