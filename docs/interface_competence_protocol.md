# Interface-competence check (protocol)

Written and frozen before any inference of this check. It is the "next necessary measurement" named in
`docs/goal_preservation_live_results.md`. That cohort is not changed or re-run.

## Question

On healthy single-goal tasks, can each local agent tell an evaluation (no effect) from a committed booking? Does
explicit effect-status wording, served by the API and in the tool descriptions, change that?

## Design

- Server and session loop: unchanged from `scripts/goal_preservation_live.py` (same system prompt, same customer
  messages, same budgets: 6 model calls, 8 tool calls, 90 s, temperature 0.3, num_ctx 4096).
- One goal per batch (customer `alice`), no other customer, preservation server. No contention exists.
- Choice sets: `[9]`, `[15]`, `[9, 15]`. Seeds 101 to 104. Models: `llama3.1:8b`, `qwen3.5:4b` (digests as in the
  goal-preservation cohort; a mismatch stops the run before inference).
- Arms:
  - `original`: the cohort's tool descriptions and responses.
  - `explicit`: the evaluate tool says it books nothing and only commit books; commit says it is the only tool that
    creates a booking. Every evaluate and commit response gains an `effect` field from the server:
    `"none: evaluation only, nothing was booked; call commit to book"` for evaluate,
    `"booked"` for a successful commit, `"none: nothing was booked"` for a failed commit.
- 2 models x 2 arms x 3 choice sets x 4 seeds = 48 sessions, in a fixed shuffled order (seed 20261103).

## Measures

- SQL-completed: a booking for `alice` exists in the batch's SQLite file, on an acceptable slot.
- False booking claim: the session ends with a `finish` message or final text that claims a booking
  (`booked`, `confirmed` or `scheduled`, with no `not`, `unable`, `cannot`, `could not`, `couldn't` or `no `) while
  SQL has no booking. The rule is mechanical. Every flagged and unflagged final message is listed for reading.
- Evaluate-then-stop: at least one evaluate call, no commit call.

## Reading

- Descriptive only; 12 sessions per model and arm. No significance test.
- A model "distinguishes evaluation from commit" in an arm if SQL-completed is at least 10 of 12 and false booking
  claims are 0.
- If Llama completes in `explicit` but not `original`, the cohort's Llama result was an interface-wording failure,
  not a model ceiling. If Llama fails both, the wording does not explain it.

## Rules

Every session is kept. No changes after the first inference; any change is a new labeled run. Local Ollama only,
no paid inference.
