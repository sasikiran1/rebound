# Local-model recovery campaign protocol (ollama-recovery-v1)

Written 2026-10-28 before any campaign trial. Frozen by the commit that adds the frozen specs file
(`manifests/ollama/blocks_v1.specs`); that commit's hash is recorded in `docs/OLLAMA_FREEZE.md` before block 1.
Nothing below changes after the freeze. A change means a new configuration, reported separately.

## Question

Does Rebound's admission gate improve backend recovery after the declared database-link latency fault without
losing correct business work, for live local-model agents? Is any effect specific to agents, or the same with
background traffic alone?

## Fixed design

| Factor | Value (all conditions) |
|---|---|
| Fault | Toxiproxy latency toxic on the `pg` proxy (API to PostgreSQL), downstream, 400 ms ± 50 ms, at 180 s for 10 s; verified by a 1 Hz canary through the proxy (rise ≥ 70% of 400 ms) and removed by the controller |
| Gate (when on) | 12 tokens, queue 24, queue wait 1.5 s, 503 + Retry-After 2 s (the setting chosen in explore-v1; not re-tuned) |
| Background | 520 req/s open loop, 70% day availability / 30% appointment reads, 2 s timeout, no retries, seed fixed. Declared, identical in every trial, and the same rate as the original calibration (about 49% of measured capacity) |
| Agents | live Ollama model via `harness/ollama.py`; prompt `prompts/agent_v1.md`; tools `prompts/tools_v1.json`; tool-result cap 4,096 bytes; full-history continuation; temperature 0, seed 41, num_ctx 16,384, num_predict 2,048, thinking off; 16 model calls, 14 tool calls, 240 s, 80,000 cumulative input tokens per goal; at most 24 concurrent goals |
| Arrivals | seeded Poisson, seed 7, 0–370 s, rate set by the rule below and identical for both models |
| Timeline | warm-up 0–60 s, baseline 60–180 s, fault 180–190 s, observation 190–370 s (180 s), drain ≤ 240 s after 370 s. Shortened from the original 360 s observation, decided before any campaign trial from explore-v1, where every trial recovered within 69 s; slower recoveries are reported as `not_recovered_within_180s` |
| Data | fresh clone of `fielddesk_template` per trial (checksum verified); pilot on task set A, blocks on task set B |
| Runner | CPUs 8–15; postgres 2–3, fielddesk 4–5, toxiproxy 6; no Meta key in the environment |

Arrival-rate rule (declared before the measurement): rate = 0.35 / (C × L), rounded down to 0.01 goals/s,
where C = median model calls per goal and L = median model-call latency of the slower model, both measured in the
`ctx16k` semantic runs (single goal, no fault). Purpose: keep the single-request Ollama server at about 35%
utilization so model queueing does not dominate. The pilot checks it (validity rule V4).

## Conditions, per model, plus a background-only control

| Code | Clients | Fault | Gate |
|---|---|---|---|
| A | agent | off | off |
| B | agent | on | off |
| C | agent | off | on |
| D | agent | on | on |
| B_bg | background only | on | off |
| D_bg | background only | on | on |

Models: gemma4-12b-16k:latest and llama3.1:8b (Ollama library; no thinking field sent). qwen3.5-9b-16k was planned
but removed before any campaign trial: in the ctx16k semantic runs its median model call took 12.3 s (about 27
output tokens/s, 100–400 visible tokens per call), which under the rate rule allows about 2 goals per trial. The
first replacement, the Hugging Face GGUF of Llama 3.1 8B, was dropped before any campaign trial because through
Ollama it wrote tool calls as plain text after its first call (semantic_hf_v1). I then chose the Ollama
library build of the same weights; it enters the campaign only if its semantic runs show working multi-step tool
calls (at least one correct booking and one correct deferral among the TECH and FULL cases).
Outcome of that check (sem_llama31_v1, before any campaign trial): 0 of 12 correct; after its first call it wrote
tool calls as plain text. **The campaign therefore runs gemma4-12b-16k only**: one block = A, B, C, D, B_bg, D_bg
(6 trials), pilot = gemma4 B and D on task set A.
One block = these 10 trials in an order shuffled with seed 20261028 + block number. Pilot = Llama 3.1 8B, conditions
B and D only, on task set A (validates fault, guards, model pacing and the gate path). Then three frozen blocks on
task set B. Cool-down between trials: 30 s plus the CPU idle check.

## Measures (per trial; definitions in analysis/metrics.py and analysis/ollama_campaign.py)

- Recovery time T_rec: healthy at second t if, over requests ARRIVING in [t − 10, t) and samples in that window:
  background success ≥ B_s − 0.01, background p95 ≤ max(1.5 B_p95, B_p95 + 20 ms), mean in-flight ≤ B_inf + 2,
  mean pool waiters ≤ B_wait + 1 (B_* from the same trial's 60–180 s baseline). T_rec = first second from t_off
  that starts 60 consecutive healthy seconds. Because the window trails by 10 s, a system that recovers the instant
  the toxic is removed still scores about T_rec = 10 s; differences below about 2 s are not interpretable.
  Fault-off trials use the nominal t_off = 190 s (a negative control: they should score the floor).
- Lost background service D (10 s windows over the observation window) and successful background requests in the
  fault window and the first post-fault minute.
- Goals: correct / scheduled (S), incorrect, duplicate, wrongly deferred, failed, unfinished, rejected,
  provider_failed. The oracle checks dates, service, skill, region, single occupancy and one change.
- Goal HTTP attempts, 409s, gate 503s, client timeouts; requests sent before Retry-After expired.
- Exposure: goal share of in-flight request-seconds and of DB round-trip ms in the fault window. DB round-trip ms
  include the injected latency; postgres CPU-seconds per second (cgroup) are reported as the DB work measure.
- Model latency p50 (baseline vs observation), `length` stops, context exhaustion, provider failures; runner lag
  and CPU; container throttling.

## Validity (a trial failing any is kept, labeled and excluded from comparisons)

V1 the harness's own rules (preflight, data checksum, baseline guard, fault verified when on, runner lag p99 < 50 ms,
runner CPU p95 < 70%, no postgres/fielddesk throttling, clock check, SHA256SUMS). V2 provider_failed ≤ 5% of goals.
V3 observation-window model latency p50 ≤ 2 × baseline p50. V4 (pilot) the same, and goal median duration
under 120 s in A.

## Decision rules (per model; a "block pair" is B vs D, or A vs C, in the same block; all valid)

1. Gate unnecessary: B is `fast` (T_rec ≤ 30 s) in at least 2 of 3 blocks. Then report that the gate is not needed
   in this regime, whatever D shows.
2. Gate helps recovery: in at least 2 of 3 blocks, D's T_rec is at least 5 s shorter than B's, or D's lost service D
   is at least 10% and at least 500 requests lower; and in every such block correct goals(D) ≥ correct goals(B) − 1
   and incorrect + duplicate (D) ≤ (B).
3. Gate hurts: in at least 2 of 3 blocks, correct goals(D) < correct goals(B) − 1, or lost service D(D) exceeds D(B)
   by at least 10% and 500 requests, or C's baseline background p95 exceeds A's by more than 5%.
4. Otherwise: inconclusive.
5. Agent-specific: a gate effect counts as agent-specific only if (D − B) in agent trials differs from
   (D_bg − B_bg) in the same block by more than the spread of (D_bg − B_bg) across blocks, in at least 2 of 3 blocks.
   Otherwise the effect (or its absence) is attributed to the background load the gate sees.

## Stop rules

No setting in the table above is changed after the freeze. The pilot may be repeated at most twice, and only for
infrastructure faults (crash, disk, a wrong file), never because of an outcome. If more than a third of block
trials are invalid, stop and report a measurement result without the comparison. Tool-information fixes
(pagination, time filters) are evaluated only in the semantic cases (`manifests/ollama/semantic_*.json`) and are
never credited to the gate.
