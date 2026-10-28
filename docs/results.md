# Results log

Numbers only. Each day appends what was measured.

## Day 1 · Tue Oct 27, 2026

### Environment (step 0)

| Item | Value |
|---|---|
| Host | Windows 11 Home 10.0.26300, Intel Core Ultra 9 275HX (8 P + 16 E cores, no SMT) |
| WSL | Ubuntu **26.04 LTS**, kernel 6.6.87.2-microsoft-standard-WSL2 |
| `nproc` (WSL) | 24 |
| Memory visible to WSL | 15 GiB total, 14 GiB available |
| Docker | Engine 29.2.1 (Docker Desktop, WSL integration), Compose v5.1.0, `hello-world` OK |
| Python / uv (WSL) | 3.12.16 (installed by uv) / uv 0.13.6 |
| Repo location | `~/rebound` (Linux filesystem) |

Proposed CPU split (vCPU indices inside the WSL VM):

| Component | cpuset |
|---|---|
| postgres | 2-3 |
| fielddesk | 4-5 |
| toxiproxy | 6 |
| runner (`taskset`) | 8-15 |
| left free (kernel, dockerd, desktop) | 0-1, 7, 16-23 |

Note: the CPU is hybrid (P + E cores). Hyper-V schedules WSL vCPUs onto host cores, so pinning inside
the VM does not guarantee a container stays on P-cores. This is a known variance source; baseline p95
stability across healthy runs (Day 4 check) is what decides whether it matters.

Project renamed from "Aftershock" to **Rebound** on Day 1 (identity headers `X-RB-*`, cache key `rebound-agent-v1`).

Docker Desktop pulls through its internal proxy (`http.docker.internal:3128`) failed intermittently with
blob-download EOFs; 3-6 retries succeeded. Direct downloads from WSL were fine. Images are pinned by digest
and cached locally, so this does not affect trials.

### Images (pinned by digest)

| Image | Digest |
|---|---|
| postgres:16 (16.15) | `sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94` |
| ghcr.io/shopify/toxiproxy:2.12.0 | `sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e` |
| python:3.12-slim (fielddesk base) | `sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f` |

### Data (1/10 scale, seed 1729)

| Table | Rows |
|---|---|
| regions / region_distances | 20 / 400 |
| skills / service_types | 6 / 10 |
| technicians / technician_skills / technician_regions | 40 / 97 / 125 |
| customers | 4,000 |
| slots | 13,440 (21 days × 16 × 40) |
| appointments | 10,166 (~70% of slots scheduled, ~5% cancelled records) |

Template checksum `sha256:f2fc24ee7d413b77f65e3cbcedcbc02f17119f897c4568fe2415251ea1e9a298`; identical on two
generator runs; clone verification passes. Build time 0.6 s.

Task set A (dev): 40 tasks = 24 one-day, 12 three-day, 4 infeasible. An independent SQL check confirmed every
task's free-candidate count and appointment identity (0 mismatches). Tasks are non-interfering: disjoint free
candidates, and no task's original slot qualifies for another task's window.

### Per-endpoint DB time (healthy, sequential, n = 100 each, 1/10 scale)

| Route | DB ms p50 | DB ms p95 | Handler ms p50 | Queries |
|---|---|---|---|---|
| GET /customers/{id}/appointments | 1.17 | 1.59 | 1.58 | 2 |
| GET /appointments/{id} | 0.83 | 1.08 | 1.24 | 1 |
| GET /availability (one day) | 1.16 | 1.58 | 1.73 | 2 |
| POST /appointments/{id}/reschedule | ~2.6-3.0 (smoke test, n = 3) | | | 4-6 |

DB time includes the round trip through toxiproxy and asyncpg's connection reset when a connection goes back
to the pool (one more round trip per request). At this scale all reads are dominated by round trips, not query
work. The ≥ 10× broad-search target is a Day 2 check, after `search_availability` and full-scale data exist.

### Latency pulse (`make pulse`: 400 ± 50 ms downstream on `pg`, 10 s; 1 Hz; medians)

| Phase | Canary ms | FieldDesk client ms | FieldDesk DB ms |
|---|---|---|---|
| before (10 s) | 1.2 | 5.5 | 2.6 |
| during (10 s) | 392.7 | 1180.7 | 1177.3 |
| after (10 s) | 0.7 | 4.1 | 1.7 |

Canary rise 391.5 ms = 98% of the configured 400 ms; it passes the ≥ 70% fault-verification guard. The
`/availability` request makes 3 round trips (2 queries + pool reset), so its DB time rose by ~3 × 400 ms. Before
the pool reset was counted, DB time covered only 809 of the 1222 ms client latency during the fault; now it
covers 1177 of 1181 ms. Recovery after the toxic was removed was immediate at 1 Hz resolution with no load.

### Offline pipeline check (no model)

A scripted stand-in for the model drove the real adapter → FieldDesk → oracle path on a fresh clone:
- one-day task A0001 → rescheduled → oracle `correct`; completion time taken from `appointment_changes.applied_at`.
- infeasible task A0003 → deferred → oracle `correct`.
- 6 `http.attempt` client events and 6 backend rows, all 6 joined by `attempt_id`.

Tests: 49 passing (oracle verdict classes, adapter validation and scope, provider retry table, agent loop limits).

### Model verification (`make verify-model`, muse-spark-1.3)

| Call | Latency ms | Input | Cached | Output (reasoning) | Est. cost |
|---|---|---|---|---|---|
| 1: minimal call, one tool | 2,931 | 574 | 0 | 110 (40) | $0.00119 |
| 2: `previous_response_id` + `function_call_output` | 14,164 | 696 | 497 | 111 (76) | $0.00080 |

- Key, model ID and Responses API work. Output items: `reasoning`, `message`, `function_call`.
- Continuation works with `store=True` set explicitly on each call.
- Usage fields: `input_tokens`, `input_tokens_details.cached_tokens`, `output_tokens`,
  `output_tokens_details.reasoning_tokens`. `cache_write_tokens` is present but null.
- **Contradicts the design:** with `previous_response_id`, `input_tokens` covers the **whole
  conversation** (696 = 574 from turn 1 plus the new turn), not just the new turn. Summing per-call usage
  therefore already counts the full billed input, and the 80,000-token per-goal cap counts the same way. Still to
  check against the billing dashboard.

### Live agent goal (`make goal TASK=A0001`, muse-spark-1.3)

Task A0001 (narrow_1day, appliance_install, customer 1441): fresh clone, FieldDesk restarted, then one live goal.

| Step | Model ms | Input (cached) | Output (reasoning) | Tool | Tool result | Tool ms |
|---|---|---|---|---|---|---|
| mc_0001_01 | 2,739 | 1,164 (0) | 91 (10) | get_customer_appointments | ok | 11.2 |
| mc_0001_02 | 10,087 | 1,580 (1,137) | 169 (50) | check_day_availability | ok | 11.3 |
| mc_0001_03 | 13,370 | 2,186 (1,521) | 159 (62) | reschedule_appointment | ok | 17.2 |
| mc_0001_04 | 8,225 | 2,368 (2,161) | 79 (6) | get_appointment | ok | 7.6 |
| mc_0001_05 | 10,528 | 2,533 (2,289) | 84 (23) | (final text) | | |

- Ended `rescheduled` after 45.0 s: 5 model calls, 4 tool calls, 0 errors, 0 transport retries.
- Tokens: input 9,831 (cached 7,108, 72%), output 582 (reasoning 151). **Estimated cost $0.00694.**
- **Oracle: `correct`**; completion time from `appointment_changes.applied_at`.
- Model step latency: median 10.1 s (range 2.7–13.4 s). Tool latency 7.6–17.2 ms. Model time is > 99% of goal time.
- Events: 4 client `http.attempt` events, all 4 joined to backend rows on (`trial_id`, `attempt_id`).
  `attempt_id` alone is not unique across trials because the backend log carries over between runs. Joins must
  include `trial_id` (Day 2: copy each trial's backend rows into its run folder).
- Day 1 model spend: 7 calls, ≈ $0.009 estimated.

Rough projection (one goal, not a measurement): at ~45 s per goal, 0.4 goals/s gives ~18 concurrent agents
(target 8–16, cap 24), and ~220 goals × $0.007 ≈ $1.5 per agent trial.

### Design interpretations (please confirm)

1. **At most one write per goal** is read as at most one *applied* write. After a 409, timeout or 503 the model
   may call `reschedule_appointment` again with the same derived idempotency key. If the earlier attempt
   actually committed, the backend returns that original change, never a second one.
2. **Ownership check:** the adapter adds the goal's `customer_id` to the reschedule body; the model never sets it.
3. **`get_appointment` scope:** allowed for the goal's appointment and for any appointment the customer listing
   returned.
4. **The backend validates technician skill and coverage** on reschedule (422 `unqualified_slot`). So `incorrect`
   verdicts can only come from date-window violations, extra writes, or writes on infeasible goals.

## Day 2 · Tue Oct 27, 2026 (continued in the same session)

### Full-scale data (seed 1729, scale 1.0)

| Table | Rows |
|---|---|
| technicians / technician_skills / technician_regions | 400 / 1,003 / 1,187 |
| customers | 40,000 |
| slots | 134,400 (21 days × 16 × 400) |
| appointments | 105,030 (incl. blocker appointments for infeasible tasks) |

Template checksum `sha256:cc0173457eef08430a5f88786196913195aa9d89c26ecf2033c27dfd0eb1beb3`, build 8.0 s.
Task sets A (task seed 101) and B (task seed 202): 300 tasks each, 180 one-day / 90 three-day / 30 infeasible,
no shared customer or appointment. Independent SQL check (`make tasks`): 0 problems in 600 tasks.

**Contradiction with the Day 1 design (recorded, design relaxed):** the Day 1 generator required every task's free
candidate slots to be disjoint from every other task's. At full scale one set would need about 43k disjoint free
slots and only about 40k slots are free, so that rule cannot hold for ~250 tasks. Kept: the design's rule (no two
tasks in a set share a (region, service, date)), infeasible tasks stay infeasible whatever other goals do, and
blocker appointments never shrink another task's candidates. Consequence: concurrent goals can race for one slot.
Measured in the healthy scripted calibration trial: 6 HTTP 409s in 228 goals, all resolved by the re-check.

**Availability is public (changed from Day 1).** the design scopes reads to "the goal's customer and region
set, plus public availability". The adapter now allows any region for `check_day_availability`,
`search_availability` and `list_technicians`; customer and appointment reads and the write stay scoped to the goal.

### Per-endpoint DB time (healthy, sequential, n = 100 each, full scale, `plan_cache_mode = force_custom_plan`)

| Route / shape | DB ms p50 | DB ms p95 | × day lookup |
|---|---|---|---|
| GET /customers/{id}/appointments | 1.82 | 2.43 | 0.7 |
| GET /appointments/{id} | 1.14 | 1.53 | 0.4 |
| GET /availability (one day) | 2.65 | 3.33 | 1.0 |
| GET /technicians | 9.51 | 12.11 | 3.6 |
| search: goal window, own region | 9.47 | 14.82 | 3.6 |
| search: goal window, own + adjacent regions | 15.69 | 22.91 | 5.9 |
| search: goal window, all regions | 23.26 | 36.28 | 8.8 |
| search: 3-day window, all regions | 29.57 | 46.26 | 11.2 |
| search: 7 days, all regions | 35.78 | 50.06 | 13.5 |

- The broad search's cost is dominated by query planning (about 10 ms for the 8-relation join). Under the default
  `plan_cache_mode = auto`, asyncpg's cached statements flipped between custom and generic plans after 5 runs and
  broad-search cost was bimodal (own region 15 ms, own + adjacent 6 ms). `force_custom_plan` was set in the frozen
  `postgres.conf` for stable per-request cost; it raised the day lookup from 1.58 to 2.65 ms.
- **≥ 10× target:** met for multi-day all-region searches (11.2× for 3-day windows). The mixed 1- and 3-day goal
  window over all regions is 8.8×, just below. Not tuned further. The scripted fallback uses the all-region shape.

### Toxiproxy overhead (no toxic, n = 300 each)

SELECT 1: 0.28 ms direct vs 0.38 ms proxied (p50, +0.10 ms). Day-count query: 0.73 vs 0.60 ms (within noise).

### Capacity step test (background only, 30 s steps, `runs/calibration/steptest_20261027T180416`)

| Offered rps | p50 ms | p95 ms | p99 ms | errors | DB ms/s |
|---|---|---|---|---|---|
| 100 | 4.48 | 6.38 | 7.06 | 0% | 294 |
| 300 | 4.00 | 6.37 | 7.72 | 0% | 853 |
| 500 | 4.00 | 7.38 | 9.94 | 0% | 1,606 |
| 600 | 4.19 | 8.46 | 11.80 | 0% | 2,183 |
| 700 | 4.33 | 9.39 | 13.84 | 0% | 2,766 |
| 800 | 4.88 | 11.84 | 22.70 | 0% | 3,737 |
| 900 | 6.61 | 42.77 | 82.09 | 0% | 5,614 |
| 1000 | 7,314 | 9,620 | 15,838 | 90% | 9,450 |

Knee at 900 rps (p95 above 2 × the 6.38 ms floor). **Capacity: 800 rps, 3,737 DB-ms/s.**
A first run showed 0.03–0.4% connection errors at low load: the background client reused keep-alive connections
that uvicorn had closed after its idle timeout. Fixed by resending once on a fresh connection when a reused
connection returns no bytes (standard HTTP-client behavior; the server never saw the request).

### Runner health fix

With httpx for background traffic, runner CPU p95 was 83–85% during the fault (hundreds of requests in flight;
httpx's pool bookkeeping grows with open connections), failing the < 70% rule. uvloop alone: 83%. A minimal
HTTP/1.1 client with an O(1) keep-alive free list, for background traffic only: **CPU p95 6%, loop-lag p99 2.2 ms**
in the same smoke trial. Agents and the scripted client still use httpx.

### Calibration step 2: scripted client, no fault, background 550 rps (`runs/calibration/tr_0001`)

- Valid. 228 goals scheduled, 228 `correct`. Runner CPU p95 17%, loop-lag p99 2.9 ms, no CPU throttling.
- Background baseline p95 8.90 ms, success 100%. 11 seconds of the 360 s window were unhealthy with no fault at all
  (noise against the healthy thresholds); T_rec 1 s.
- Baseline DB time: **2,047 ms/s total = 54.8% of capacity**; goals 10.3 ms/s; background 2,037 ms/s.
- Background lowered to **520 rps** for the remaining trials to centre total healthy DB time near 50%.

**Contradiction with the design, step 4 (recorded; cannot be met within the design):** goals are **0.5%**
of healthy DB time, not ≥ 30%. A goal costs about 25 ms of DB time in total, and agents pace their steps by model
latency (about 10 s per step), so even 24 concurrent agents send only a few requests per second. Reaching 30% at a
50%-of-capacity load would need about 800 DB-ms/s from goals, i.e. tens of goals per second, far beyond the 24-goal
concurrency cap and the model's step latency. Kept: the ordinary load (50% of capacity), because that defines the
question. Every report states this scale-model limit. It means agent behavior can move recovery only if its
reactions cost many times more than its healthy traffic.

## Exploration (task set A, `runs/explore-v1`)

### First fault trials, gate off, background 520 rps (10 s, 400 ± 50 ms fault)

| | tr_0001 scripted | tr_0002 agents (Spark 1.3) |
|---|---|---|
| Status | valid | valid |
| Baseline total DB time | 1,826 ms/s (48.9% of capacity) | 1,761 ms/s (47.1%) |
| Goal share of baseline DB time | 0.56% | 0.74% |
| Background baseline p95 | 8.38 ms | 7.66 ms |
| Canary rise | 408 ms | 413 ms |
| Recovery | fast, T_rec 23 s | fast, T_rec 20 s |
| Unhealthy seconds in the 360 s window | 32 | 19 |
| Lost background service D | 9,048 | 7,733 |
| Goals scheduled / correct | 228 / 221 (7 `failed`) | 228 / 228 |
| HTTP attempts per goal: baseline / fault / post cohort | 4.26 / 4.07 / 3.81 | 4.10 / 4.45 / 3.90 |
| DB ms per goal: baseline / fault / post | 24.8 / 33.3 / 25.4 | 30.6 / 37.4 / 30.5 |
| Reactions (fault cohort) | 1 switch to broad search, 2 other; 35 scripted retries | 11 same-call retries, 2 switches to broad search, 3 other |
| Transport retries in the agent path | – | 0 |
| Runner CPU p95 / loop-lag p99 | 17.6% / 2.8 ms | 19.2% / 6.0 ms |

- The 7 scripted failures all arrived during the fault: three 3 s timeouts on the first read (initial attempt + 2
  retries), then the policy gives up (its only fallback is for day lookups).
- Agents: 1,151 model calls, 3.53 M input tokens (2.51 M cached, 71%), 167k output (57.8k reasoning).
  **$2.36 per trial, $0.0104 per goal.** Model latency p50 9.74 s (baseline 9.72 s, observation 9.75 s).
  3 provider retries, 0 provider failures.
- **Agents' median healthy step time: 10.25 s** (gaps between consecutive tool operations, baseline cohort). Frozen
  as the scripted think time (was 10.1 s from the Day 1 single goal).
- Trial wall time about 10 minutes: every goal ended well before the 790 s drain deadline.
- Baseline background p95 across the three healthy-load runs so far: 8.90 (550 rps), 8.38, 7.66 ms (520 rps):
  9% spread at 520 rps, inside the 15% Day 4 check.

### Budget gate

Measured $2.36 per agent trial. Remaining plan: 2–3 exploratory agent gate-on trials, 1 healthy reference, 6
headline agent trials, 4 unfamiliar-fault trials ≈ 14 × $2.4 ≈ $34, plus $2.36 spent and $0.01 on Day 1.
Projected total about $37, under the $100 gate; no reductions needed.

### Gate grid, scripted client, 10 s fault, think time 10.25 s

| Trial | Gate | Baseline bg p95 | Δ vs gate-off mean 7.94 ms | Baseline rejections | T_rec | D | correct / rejected / failed |
|---|---|---|---|---|---|---|---|
| tr_0001 | off (think 10.1 s) | 8.38 ms | – | 0 | 23 s | 9,048 | 221 / 0 / 7 |
| tr_0003 | off | 7.50 ms | – | 0 | 20 s | 7,846 | 223 / 0 / 5 |
| tr_0004 | 8 tokens, 1.0 s | 8.57 ms | +7.9% | 0 | 10 s | 5,025 | 223 / 3 / 2 |
| tr_0005 | 8 tokens, 1.5 s | 8.40 ms | +5.8% | 0 | 10 s | 5,134 | 223 / 3 / 2 |
| tr_0006 | 12 tokens, 1.0 s | 10.16 ms | +28% | 0.76% | 10 s | 5,018 | 223 / 3 / 2 |
| tr_0007 | 12 tokens, 1.5 s | 8.09 ms | +1.9% | 0 | 10 s | 5,056 | 223 / 3 / 2 |

- Every grid point halved T_rec (20 → 10 s) and cut D by about a third with the same correct count. Baseline goal
  success was 100% in all six trials, so the overhead rule is decided by background p95.
- Gate-off baselines alone differ by 11% (7.50 vs 8.38 ms), so differences under about 10% are within run-to-run
  noise. The rule (smallest healthy-period overhead) picks **12 tokens, 1.5 s queue wait** (+1.9%, the only point
  within 5%). Runner-up 8 tokens / 1.5 s. Both get one exploratory agent gate-on trial for the recovery tie-break.
- During the fault the gate rejected about 5,000 requests (503 + Retry-After 2) and queued about 1,400.

### Host disk full (tr_0009, tr_0010) — kept, both INVALID

The Windows C: drive (which holds the WSL and Docker virtual disks) reached 0 bytes free during the two exploratory
agent gate-on trials. WSL then returned I/O errors for every command and Docker Desktop stopped responding.
- tr_0009 (gate 12 tokens / 1.5 s): INVALID by the baseline guard ("baseline unhealthy": background p95 18.5 ms vs
  the 13.35 ms limit, success 99.63%), most likely from the filling disk. Files intact; $0.78 model spend.
- tr_0010 (gate 8 tokens / 1.5 s): the runner died in OBSERVING; no oracle, analysis or checksums. Marked INVALID
  with `python -m harness mark-crashed` (all files kept); $1.80 model spend from its model_calls.jsonl.
- After the user freed space (330 GB free), WSL and Docker were restarted and both trials re-run. Preflight now
  requires ≥ 10 GB free on both `/` and the host drive (`/mnt/c`).

Model spend to this point: $2.37 (Day 1 + tr_0002) + $0.78 + $1.80 = $4.95.

### Agent gate-on re-runs (tr_0011, tr_0012) — kept, both INCONCLUSIVE (provider)

| Trial | Gate | T_rec | D | correct / provider_failed | Baseline bg p95 | Baseline rejections | Model $ |
|---|---|---|---|---|---|---|---|
| tr_0002 | off | 20 s | 7,733 | 228 / 0 | 7.66 ms | 0 | 2.36 |
| tr_0011 | 12 tokens, 1.5 s | 10 s | 5,015 | 213 / 15 | 7.66 ms | 0.06% | 2.20 |
| tr_0012 | 8 tokens, 1.5 s | 10 s | 5,092 | 214 / 14 | 6.91 ms | 0 | 2.22 |

Both are inconclusive under the frozen rule (> 5% of goals `provider_failed`). Every provider failure (36 and 43
calls) was HTTP 400 "invalid_request_error referenced response not found or expired". They were spread over the
whole trial (32 s to 555 s), not tied to the fault. Each failing continuation referenced a response created only
0.8–3.6 s earlier (median 1.4–1.6 s), i.e. on turns 3–5 right after a tool call that took milliseconds; successful
continuations referenced responses with a median age of 9.3 s (up to 47 s). So this is a read-after-write race in
the provider's response store, not expiry. It did not occur on Day 1 or in tr_0002 earlier the same day.

**Contradiction with the design (provider table: 400 → no retry), changed:** the provider wrapper now
treats this one 400 like a 503: up to 3 retries after fixed waits of 1, 2 and 4 s, recorded as provider retries and
provider wait. Any other 400 still ends the goal as `provider_failed`. This is a model-provider retry, not a
transport retry to FieldDesk, so rule 2 still holds. Validated in tr_0013 before the freeze.

Gate choice for the freeze, by the section 7a rule: **12 tokens, queue max 24, queue wait 1.5 s** (smallest scripted
healthy-period overhead, +1.9%). In the agent trials both candidates gave the same recovery (T_rec 10 s vs 20 s with
the gate off) and nearly the same D (5,015 vs 5,092), so the agent tie-break did not change the choice.

### Replay mechanics test (tr_0008)

Replay of tr_0002's agent trace (which includes that trial's fault reactions; mechanics only, not a control):
valid, 228/228 correct, 940 requests replayed open-loop, attempts per goal in the fault cohort 4.45 (identical to
the source trace), T_rec 21 s, D 8,428.

