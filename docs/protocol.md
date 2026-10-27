# Rebound evaluation protocol (frozen before evaluation)

This file fixes, before the first evaluation trial, every rule the evaluation is judged by. The frozen manifest is
`manifests/frozen/eval_v1.yaml`; the commit that adds both files is recorded in `docs/FREEZE.md`. After that commit
nothing that affects behavior or analysis changes; a change means a new configuration and new trials.

Code that implements each rule is named next to it.

## 1. Trial timeline

Time 0 is the moment background traffic and goal arrivals start (after PREPARING: Toxiproxy reset, database clone
from `fielddesk_template` with row-count and checksum verification, FieldDesk restart with the trial's gate setting,
preflight).

| Phase | Time (s) | Notes |
|---|---|---|
| Warm-up | 0–60 | not measured |
| Baseline | 60–180 | reference values for recovery |
| Fault | 180–190 | 400 ms ± 50 ms downstream latency on the `pg` proxy (unfamiliar condition: 180–200) |
| Observation | t_off – t_off+360 | t_off = moment the toxic is removed |
| Drain | 550 – at most 790 | arrivals stop at 550; goals still running at 790 are cancelled and are `unfinished` |

Guards (`harness/trial.py`): at fault time the backend-side baseline background success must be ≥ 99% and its p95
≤ 1.5 × the calibrated healthy p95, else INVALID ("baseline unhealthy"). During the fault the canary query through
the proxy must rise by ≥ 70% of the configured latency, else INVALID ("fault not verified").

## 2. Recovery (`analysis/recovery.py`)

Backend-side data only: FieldDesk's request log (`backend.jsonl`, background requests identified by the echoed actor
header, which the gate never reads) and its 1 Hz sampler (`samples.jsonl`).

- Background latency of a request = FieldDesk `total_ms` (gate queue + handler). A background request succeeds if
  its status is 2xx and `total_ms` ≤ 2000 ms (the background client timeout).
- Reference values from the same trial's baseline window [60, 180): `B_p95` = p95 of background latency,
  `B_s` = background success share, `B_inf` = p95 of sampled in-flight requests, `B_wait` = p95 of sampled pool
  waiters. Percentiles use the nearest-rank method.
- The trailing window for second t is [t − 10, t) (requests by arrival time, samples by sample time).
- Healthy at second t iff all of:
  1. background success in the window ≥ `B_s` − 0.01
  2. background p95 in the window ≤ max(1.5 × `B_p95`, `B_p95` + 20 ms)
  3. mean sampled in-flight in the window ≤ `B_inf` + 2
  4. mean sampled pool waiters in the window ≤ `B_wait` + 1
  A window with no background request or no sample is unhealthy.
- T_rec = (first whole second t ≥ t_off such that t … t+59 are all healthy) − t_off, with t+59 ≤ t_off + 360.
- Verdict: `fast` (T_rec ≤ 30 s), `slow` (recovered, T_rec > 30 s), `not_recovered_within_360s`.
  Never "never recovers".
- Useful-service check: goal success among goals scheduled in [t_off + T_rec, t_off + T_rec + 120) divided by the
  baseline cohort's success; flagged if below 0.8.

## 3. Goal outcomes (`oracle/verdicts.py`)

The oracle reads final database state through the read-only role on the direct port, bypassing Toxiproxy. One
verdict per scheduled goal: `correct`, `incorrect`, `duplicate_effect`, `wrongly_deferred`, `failed`, `unfinished`,
`rejected`, `provider_failed` (definitions in the design). A goal's end reason comes from the harness;
`rejected` means no effect and every HTTP attempt of the goal received the gate's 503 with Retry-After.
Completion time = `appointment_changes.applied_at` (backend commit) − goal arrival time.

## 4. Metrics (`analysis/metrics.py`)

- Every rate divides by goals scheduled. S = N_correct / N_scheduled.
- F(τ) = share of feasible goals with a `correct` commit within τ of arrival; F(60 s), F(180 s) reported.
- Cohorts by arrival time: baseline [60, 150), fault [150, 250), post [250, 550).
  R_C = client HTTP attempts per goal, W_C = backend DB ms per goal (joined on the goal header).
  Amplification = R_C(condition) / R_C(replay), gate off, 10 s fault, means over valid trials.
- Lost background service D = Σ over 10 s windows w from floor(t_on) while w + 10 ≤ t_off + 360 of
  max(0, offered_w × B_s − successes_w), offered_w = background rate × 10.
- Model cost = Σ per call (1.25·(I−K) + 0.15·K + 4.25·O) / 1e6 for Spark 1.3, with I, K, O as reported per call.
  With `previous_response_id`, per-call `input_tokens` already include the earlier turns (measured Day 1).
- Retry classes (`analysis/retries.py`): per goal, in operation order, first matching rule wins:
  `same_call_retry` (same tool and argument hash as the immediately previous operation, which failed),
  `switch_to_broad_search` (`search_availability` after a failed `check_day_availability` or `list_technicians`),
  `restart` (`get_customer_appointments` after any other tool), `other` (exact repeat of an earlier call, a second or
  later search, or a write after a failed write), else `base`. Invalid-argument calls are counted separately.
  Transport retries (attempt_n > 1) must be zero for agents; any is a bug and makes the trial invalid.

## 5. Validity (`analysis/metrics.py`, `harness/trial.py`)

A trial is **invalid** if: preflight fails (FieldDesk health, no toxics, canary, CPU separation between the runner
and the postgres/fielddesk cpusets, model key or replay trace present); the data checksum or task-file hash differs
from the manifest; the baseline guard or fault verification fails; runner event-loop lag p99 ≥ 50 ms; runner CPU
(1 s windows, share of one core) p95 ≥ 70%; any postgres or fielddesk CPU throttling (cgroup `nr_throttled` delta
> 0); any agent transport retry; more than 1% of backend records fall outside their client attempt (clock check).
A trial is **inconclusive** if a goal lacks an oracle verdict; for agents, if more than 5% of goals end
`provider_failed`, or median model latency in the observation window is more than 2× the baseline-window median.
A trial is **cancelled** when its model spend passes the $10 cap. Trials whose files do not match `SHA256SUMS` are
invalid. Every trial is kept with its state and reasons.

## 6. Evaluation design

- Task set B, arrival seed 7 for every trial, generator seed 1729.
- Headline: 5 cells × 3 repeats in 3 blocks: agent gate-off, agent gate-on, scripted gate-off, scripted gate-on,
  replay gate-off. Order inside each block is a seeded shuffle (seed recorded in `docs/FREEZE.md`).
- Replay trace: one healthy reference agent trial (no fault) on set B before block 1.
- Unfamiliar condition: 20 s fault, agent gate-off and gate-on (two each if budget allows, one minimum).
- Between trials: full reset (always), 60 s cool-down.

## 7. Gate acceptance criteria (`analysis/experiment.py`), judged on agent cells

1. Healthy period: gate-on baseline-cohort goal success and baseline background p95 within 5% of gate-off;
   baseline-window rejections ≤ 1% of requests.
2. Recovery: in ≥ 2 of 3 paired blocks, T_rec with the gate is shorter, or both are `fast` (then the report says the
   gate was not needed). A trial that did not recover counts as T_rec = ∞.
3. Useful work: per block, correct goals gate-on ≥ gate-off − 3; incorrect and duplicate totals not higher.
4. Ordinary users: Σ D gate-on ≤ Σ D gate-off over the paired blocks.
5. Unfamiliar 20 s fault: the direction of 2–4 holds in the available trials; reported as weaker evidence.
