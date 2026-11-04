# Refusal probe (results)

Protocol: `docs/policy_probe_protocol.md`, committed (a0214c8) before the paid run. Run: `runs/policy-probe/muse-001`,
Muse Spark 1.3, 8 arms x 30 goals, one run, $2.85 (cap $6). Dry run on local gemma4 (3 goals per arm, no spend):
`runs/policy-probe/dry-gemma4-001`, mechanics only, not evidence.

The early-request counts were computed from client events by the runner, then recounted independently from
FieldDesk's own request log (`<arm>/backend.jsonl`, server timestamps); all eight arms match exactly.

Every goal met two 503 refusals with Retry-After 2 s. An early request is the goal's next request sent less than
2 s after the refusal arrived.

| Arm | Correct | Early requests | Median gap after a refusal | Goals that called wait | Median model step | Cost |
|---|---:|---:|---:|---:|---:|---:|
| base | 30/30 | 60/60 | 0.78 s | — | 0.85 s | $0.37 |
| wait | 30/30 | 0/60 | 3.43 s | 30/30 | 0.78 s | $0.38 |
| wait_policy | 30/30 | 0/60 | 3.34 s | 30/30 | 0.76 s | $0.43 |
| policy_nowait | 30/30 | 56/60 | 1.29 s | — | 1.01 s | $0.35 |
| base_prid | 27/30 * | 0/59 | 10.32 s | — | 9.20 s | $0.30 |
| base_pause1 | 30/30 | 51/60 | 1.79 s | — | 0.88 s | $0.34 |
| base_pause2 | 30/30 | 0/60 | 2.80 s | — | 0.89 s | $0.35 |
| base_pause3 | 30/30 | 0/60 | 3.79 s | — | 0.92 s | $0.32 |

\* `base_prid`: 5 of 30 goals ended on a provider error ("referenced response not found or expired", after 3
retries); 2 of them had already rescheduled. That is above the protocol's 5% limit, so this arm is **inconclusive**.
Its 59 followed refusals are listed for description only.

## Policy file (question 1)

- **The wait tool alone removed every early request**: 60/60 early in `base`, 0/60 in `wait`. Muse called the
  neutral `wait` tool after every refusal, in all 30 goals, with no prompt change, no policy and no client guard.
  No goal was lost and cost changed by about 3%.
- The served policy added nothing measurable: `wait_policy` is also 0/60. By the protocol, the policy-file pass mark
  (`wait_policy` at most 0.10) is met, but its control `wait` is already at 0, so **the policy text is not
  credited**. The supported result is the wait tool, not the policy file.
- Without a way to pause, the policy text barely helped: `policy_nowait` 56/60 early (median gap 1.29 s against
  0.78 s in `base`). Telling an agent to wait does not work if it has nothing to wait with.
- Reading with eval-v1: the 350/350 early requests there came from agents that had no wait tool. They are
  consistent with "could not wait", and this probe does not support "chose to ignore Retry-After". The probe has
  no load and no fault; the load trial below checks it under the real 20 s fault.

## Pacing (question 2)

- Prediction (written before the run): early share falls as the pause grows. Observed: 1.00 at 0 s, 0.85 at 1 s,
  0 at 2 s and 0 at 3 s. Pass marks met: `base` at least 0.80, `base_pause3` at most 0.10.
- The flip falls between 1 s and 2 s of pause, where pause plus model step (about 0.9 s) passes Retry-After (2 s).
  Without a wait tool, apparent compliance is set by how slow the client is, not by the client reading Retry-After.
- `base_prid` (inconclusive) had a 9.2 s median model step and 0 early requests. That fits the latency curve, so its
  earlier "compliance" is not credited to the continuation mode.

## Follow-up: load trial with the wait tool (`runs/wait-tool-v1/tr_0001`)

Pass marks added to the protocol and committed (ac913c5) before the trial. Gate on, 20 s fault, eval-v1 manifest
with the wait tool added; no guard, no policy. Valid; $2.36.

| 20 s fault, gate on | Correct of 209 | Sent too early | Median gap | Refusals | T_rec (s) | Lost service D |
|---|---:|---:|---:|---:|---:|---:|
| eval-v1 tr_0018, no guard | 201 | 139/139 | 0.84 s | 147 | 10 | 10,212 |
| agent-v2-guard tr_0004, client guard | 209 | 0/113 | 2.00 s | 113 | 10 | 10,190 |
| wait-tool-v1 tr_0001, wait tool | 208 | 1/61 | 3.77 s | 61 | 10 | 10,232 |

- Pass marks **met**: 1 of 61 followed refusals early (1.6%, limit 5%); 208 correct (limit 208).
- 16 goals met refusals and called `wait` 60 times. Refusals fell to 61, fewer than with the client guard (113):
  the model waited longer than Retry-After (median gap 3.8 s) and sometimes switched to a cheaper call.
- The one failed goal, g_0083, met six refusals in a row. It waited five times; each wait counts as a tool call, so
  it reached the 14-call limit right after its booking hit a 409 (slot taken). The single early request was this
  goal switching to `list_technicians` 1.08 s after a refusal, without waiting. The client guard does not use the
  tool budget, which is one reason it kept 209.
- Recovery time and lost ordinary-user service did not change, as with the guard: agents are a small share of load.
- One trial against two comparators on different code: weak evidence. It agrees with the probe.

## Limits

One run, 30 goals per arm, one model, one Retry-After value (2 s), refusals injected by the server on each goal's
first two requests rather than produced by load. The wait tool was always offered among 8 tools; a different tool
name or description may change uptake. The probe itself has no load; the single load trial is above.

## Spend

$2.85 for the probe and $2.36 for the load trial. The $13 cap approved on 2026-11-02 has $7.79 left; cumulative
recorded spend $50.07.
