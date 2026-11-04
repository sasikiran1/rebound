# Refusal probe: agent policy file and pacing sweep (protocol)

Written before any paid run of this study. It changes no earlier result.

## Why

In eval-v1, agents sent a new request before Retry-After expired after 350 of 350 refusals. The agent-v2 guard fixed
that in client code. Two questions remain:

1. **Policy file.** Can the API owner fix it without client code, by serving a policy, if the agent can pause at all?
   FieldDesk serves `/.well-known/agent-policy.json`, and with the policy on, each 503 states it: send nothing for
   Retry-After seconds; use a wait tool if you have one.
2. **Pacing.** Earlier runs mixed two things: `previous_response_id` runs were slow (about 9.7 s per model call)
   and rarely early; `full_history` runs were fast (about 0.8 s) and always early. Is apparent compliance just
   latency? A fixed pause before each model call separates speed from continuation mode.

## Setup (`scripts/policy_probe.py`)

- FieldDesk refuses the first 2 requests of every goal with 503, Retry-After 2 (`PROBE_REFUSE_FIRST=2`), without
  touching the database. No gate, no fault, no background traffic. Fresh database and FieldDesk restart per arm.
- Tasks: the first 30 tasks of task set B (`tasks_eval_B.jsonl`) that expect a reschedule; the same 30 in every arm.
- Agent: Muse Spark 1.3, `prompts/agent_v1.md` unchanged in every arm, reasoning low, 16 model calls, 14 tool calls,
  240 s per goal, concurrency 6. The SQL oracle judges every goal.
- Arms:

| Arm | Tools | Agent policy | Continuation | Pause before each model call |
|---|---|---|---|---|
| base | v1 | off | full_history | 0 s |
| wait | v1 + `wait` | off | full_history | 0 s |
| wait_policy | v1 + `wait` | on | full_history | 0 s |
| policy_nowait | v1 | on | full_history | 0 s |
| base_prid | v1 | off | previous_response_id | 0 s |
| base_pause1 / 2 / 3 | v1 | off | full_history | 1 / 2 / 3 s |

The `wait` tool's description is neutral ("Pause for a number of seconds before continuing. Sends no request and
changes nothing."); it does not mention Retry-After.

## Measures

- Early request: after a 503 with Retry-After, the goal's next request is sent less than Retry-After seconds after
  the 503 response arrived (client event timestamps). Early share = early requests / refusals followed by a request.
- Correct goals (oracle), end reasons, wait calls, median model step time, cost.

## Pass marks

Policy file (question 1):
- Supported if `wait_policy` early share is at most 0.10, and its correct goals are at least `base` correct minus 2.
- `wait` is the control for the policy: if `wait` alone is already at most 0.10, the policy text adds nothing
  measurable here, and that is reported as the result.
- `policy_nowait` shows what the policy text does when the agent cannot pause (e.g. more deferrals). Descriptive.

Pacing (question 2), predicted before the run: early share falls as the pause grows, near 0 once pause plus model
step exceeds 2 s. Supported if `base` early share is at least 0.80 and `base_pause3` at most 0.10. `base_prid` is
read against the curve by its median gap; its continuation mode alone is not credited with compliance if its gap
explains it.

## Follow-up: one load trial with the wait tool

Added after the probe's results (`docs/policy_probe_results.md`) and before this trial ran.

- Manifest `manifests/exploratory/wait_tool_v1.yaml`: `manifests/frozen/eval_v1.yaml` with only the experiment name
  and the tool set changed (tools_v1 plus `wait`). Gate on, 20 s fault, no runtime guard, no agent policy.
  Command: `python -m harness trial --manifest manifests/exploratory/wait_tool_v1.yaml --condition agent --gate on
  --experiment wait-tool-v1 --block 1 --label wait-20s --fault-duration-s 20`.
- Comparators, all 20 s and gate on, different code: eval-v1 tr_0018 (no guard: 201 of 209 correct, 139/139 early)
  and agent-v2-guard tr_0004 (client guard: 209 of 209, 0/113 early).
- Supported if requests sent before Retry-After expired are at most 5% of refused requests followed by another
  request, and correct goals are at least 208. One trial: weak evidence, reported as one pair with each comparator.
- Estimate about $2.40 (eval-v1 20 s trials); stops at the trial budget in the manifest ($10). Spend counts against
  the $13 cap ($10.15 left before this trial).

## Spend and rules

- Estimate: about $0.0105 per goal at the eval-v1 token mix, plus extra calls for refusals; 240 goals, about $3.20.
  Runner cap $6. This study and any follow-up share the $13 cap approved on 2026-11-02.
- A dry run on local gemma4 (3 tasks per arm, no spend) checks the mechanics before the paid run. It is kept and is
  not evidence.
- Run once. Every goal is kept. A provider failure ending more than 5% of an arm's goals makes that arm
  inconclusive. No threshold, prompt or tool changes after the paid run starts; any change is a new labeled run.
