# Goal preservation: first frozen local-agent cohort

Recorded November 2, 2026. All 48 planned batches and 96 model-backed sessions were retained. The task instances,
explicit constraints and arrival orders were constructed. Model decisions were generated live using installed
Ollama models, with real persisted SQLite writes. No paid inference, downloads, fake model responses or injected
faults were used. Test doubles were used only in separate runner unit tests, never in this evaluation cohort.

## Outcome

The prewritten requirement for a completion benefit on both chosen models was **not met**. Qwen gained two
completed goals; Llama never committed a booking in either condition. The central deterministic scheduler also
achieved the fixture maximums. These results do not demonstrate superiority over ordinary scheduling or novelty.

| Model | Ordinary SQL-completed goals | Preservation SQL-completed goals |
|---|---:|---:|
| Llama 3.1 8B | 0/24 (0%) | 0/24 (0%) |
| Qwen3.5 4B | 18/24 (75%) | 20/24 (83.3%) |

Each condition included eight sessions on insufficient-capacity cases, where at most four bookings were possible.
Thus the maximum possible total per model/condition was 20/24, not 24/24. Those uncompleted goals remain in the
denominator; they are not quietly converted into successful bookings. A finish message is separately recorded.

| Fixture | Llama ordinary / preservation | Qwen ordinary / preservation | Central reference |
|---|---:|---:|---:|
| Avoidable contention | 0/8 / 0/8 | 6/8 / 8/8 | 2/2 |
| No contention | 0/8 / 0/8 | 8/8 / 8/8 | 2/2 |
| Insufficient capacity | 0/8 / 0/8 | 4/8 / 4/8 | 1/2 |

Central references were run once per fixture without a model. Live counts cover both orders and both seeds.
Four paired batches per model/fixture are too few for a general efficacy claim. The protocol planned descriptive
comparisons, not a significance test. The two Qwen gains occurred when the flexible customer acted first.

## What actually happened

In the two affected Qwen ordinary batches, Alice took slot 15 even though slot 9 was acceptable. Bob required
slot 15 and could not book. In the matched protected batches, the evaluate response said the proposed choice
would lose an opportunity and offered slot 9; Qwen chose 9 and Bob then booked 15. These were model-selected
actions, not an automatic fallback that booked on the model's behalf.

There were **two negative evaluate responses**, **zero failed commit requests**, and **20 protected commits**.
Recovery after an actual rejected commit was therefore **not exercised**, not demonstrated. The positive
observation concerns selecting a different action after advisory feedback. The deterministic mechanics tests
exercise rejected commits separately.

Llama generally treated the evaluation result as if it had booked. One retained example in
`batch_001/alice/session.json` evaluated slot 15, received an allowed=true response with no write, then said
"Your appointment is booked at 15:00." The independent SQL state contained no booking. Its 48 zero-completion
sessions are retained. This is evidence about this tool interface and configuration, not a universal model ranking.

Overall there were 59 no_action endings and 37 explicit finish-tool endings. Some Qwen sessions ended without
calling finish after a correct write; SQL completion still counted. Zero avoidable loss in Llama's cases means
its untouched goals remained feasible, not that it completed useful work. Goal completion, feasibility and
workflow completion must stay separate.

## Time: median [minimum, maximum] seconds

| Model / condition | Per-session wall time | Per-model-decision latency |
|---|---:|---:|
| Llama / ordinary | 1.013 [0.913, 19.181] | 0.557 [0.177, 18.905] |
| Llama / preservation | 1.011 [0.892, 1.373] | 0.574 [0.193, 0.957] |
| Qwen / ordinary | 2.653 [1.483, 7.099] | 0.712 [0.277, 5.328] |
| Qwen / preservation | 2.711 [1.478, 4.074] | 0.724 [0.300, 1.298] |

Cold model loading is included. These are small-sample timings, not a measured production overhead guarantee.
The ordinary condition intentionally still ran the same internal evaluator while hiding collective advice and
disabling its enforcement, isolating the behavior change rather than benchmarking the cheapest ordinary server.

## Protocol and integrity

Protocol: `docs/goal_preservation_live_protocol.md`. Exact source hashes, model digests, prompts, tools and
randomized plan were recorded before first inference in the cohort's `freeze.json`. This was a local freeze,
not a public external preregistration. The runner used the existing Ollama relay and recorded raw model calls.
Requests to the research API used in-process ASGI HTTP; no actual network reliability experiment was performed.

A source-archive bug was discovered by the independent audit: backend and oracle files shared a basename,
and the archive flattened filenames. The initial failed audit remains in `audit.initial.json`. All working
source hashes matched before and at the end of collection. The overwritten backend source copy was recovered
afterward, verified against the original pre-inference hash and explicitly recorded in `source_recovery.json`
and `deviations.md`. Raw trial results, protocol and criteria were not changed or rerun. The recorder was fixed
after the cohort for future runs and gained a same-filename regression test.

The final audit checked all 48 batches, all 96 sessions, SQL/trace effects, paired initial model requests,
protected-commit feasibility and source hashes with the recovery disclosed. No remaining audit discrepancies.

## Supported, unsupported and required next measurement

- Supported in this cohort: Qwen used collective-feasibility feedback to preserve two otherwise lost bookings;
  no completed-goal reduction was observed in the no-contention control. This is a narrow descriptive finding.
- Not met: the prewritten completion-benefit criterion for both models. Llama did not reach committing behavior.
- Not exercised: recovery after a real commit rejection.
- Not demonstrated: benefit over central scheduling, production reliability, natural-language constraint
  extraction, fairness, privacy, cryptographic certificates or original invention status.

The next necessary measurement is a separately frozen interface-competence check: can each agent distinguish
evaluation from a committed write on healthy single-goal tasks? Compare explicit effect-status wording against
the original interface while retaining this cohort unchanged. Establish that baseline before claiming a model
cannot use preservation or expanding the contention experiment. A larger independent cohort is then needed
to estimate reproducible effects beyond these three fixed choice sets.

## Evidence and reproduction

Raw cohort: `runs/goal-preservation/live-20261102-01/` (ignored local results, not a published release).
It includes protocol, freeze, source archive, recovered source, 48 databases, per-session raw requests/replies,
tool traces, comparison.csv, summary.json, scheduler.json, initial and final audits, and generated report.md.

```sh
PYTHONPATH=backend:. .venv/bin/python scripts/audit_goal_preservation_live.py runs/goal-preservation/live-20261102-01
PYTHONPATH=backend:. .venv/bin/python scripts/goal_preservation_live.py --output runs/goal-preservation/NEW-DIRECTORY
```

The second command performs a new cohort using the current recorder; it does not reproduce identical stochastic
outputs or recreate the original archive defect. No commits or publication were performed as part of this work.
