# Goal preservation local-agent feasibility protocol, v1

Written before new inference, November 2, 2026. Separate from frozen eval-v1 and all prior cohorts.
This is an exploratory, constructed, healthy-service feasibility study. It is not a production observation,
formal preregistration with an external registry, publication-quality efficacy trial or proof of novelty.

## Fixed design

- Models: llama3.1:8b digest 46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e;
  qwen3.5:4b digest d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04.
- Existing local Ollama via existing Windows relay. No downloads, hosted inference, provider fallback or automatic retries.
- Three explicit choice-set fixtures: avoidable contention (Alice 9/15, Bob 15); no contention (Alice 9, Bob 15);
  insufficient capacity (both 15). Slot labels represent times on one requested day.
- Register both trusted goal sets before acting. Two independent sessions, sequential Alice-first and Bob-first.
  This is controlled arrival order, not simultaneous agent collaboration or a naturally occurring race.
- Seeds 101 and 102; temperature 0.3, context 4096, output cap 384, thinking disabled, full visible history,
  at most 6 model calls and 8 tool calls per session, 90-second inference-loop deadline, keep-alive 5 minutes.
- Two conditions: ordinary booking and preservation. 2 models x 3 fixtures x 2 orders x 2 seeds x 2 arms =
  48 batches / 96 live sessions. Shuffle within each model with seed 20261102; same seeds paired across arms.
- Ordinary is a matched ablation: same evaluator internally runs, but collective feasibility fields are hidden,
  alternatives absent and opportunity-loss enforcement disabled. Own constraints, unique slots, revision checks,
  expiry and one-effect-per-goal remain. Do not interpret its timing as a minimal ordinary server's overhead.
- Centralized constrained-first deterministic scheduling runs once per fixture without inference. It is a
  competent reference for these fixtures, not a representative general scheduler benchmark.
- All clients use ASGI HTTP in-process and persist real SQLite writes. No network faults or artificial outages.
- Shared system prompt and tool definitions reside in the runner, copied into the pre-inference freeze manifest.
  Customer prompt: Alice in contention prefers 15:00 but accepts 09:00; singleton choices require their exact time.
  No instructions force a failed booking, retry, alternative selection or handoff. Models choose actions.

## Definitions and outcomes

SQL correctness: exactly one booking for the session's registered goal in its allowed slot set, with no duplicated
slot. Do not infer completion from the model's report. Session end is separately retained: finished, no_action,
protocol_error, timeout, call_budget, tool_budget or runner_or_provider_error. A correct SQL booking counts even
when the model fails to finish. Report end-state and workflow completion separately. Keep every session.

Batch optimum: independent exhaustive enumeration over registered fixture choices. Avoidable loss is initial
maximum completions minus committed completions plus maximum remaining completions (the whole attainable total).
Correct booking count, residual feasible count and lost opportunity are distinct. The insufficient-capacity case
cannot complete both goals; correctly avoiding duplicates does not make the missing booking a completed goal.

Primary descriptive comparison: paired batch difference in SQL-completed goals, preservation minus ordinary.
Break down by fixture/model/order; never pool tool calls as independent observations. Record all raw model
requests/replies, tool responses, SQL databases, model/tool counts, server errors, recovery after rejection,
session duration and per-decision latency. Report median and range; cold loading is included and may affect time.
No significance claim is planned for this small repeated-fixture cohort. Seeds are not independently sampled users.

## Pre-written failure criteria

1. Mechanics claim fails if protected commits produce an invalid/duplicate effect or reduce the independent
   current maximum attainable count. Keep the failure and inspect it; do not modify the endpoint mid-cohort.
2. A descriptive completion-benefit claim requires a positive paired total on avoidable-contention cases for
   each model, with no reduction on no-contention cases. Otherwise label it unsupported or mixed, not proven safe.
3. Claim that agents can recover from a protection rejection requires an observed real rejection followed by a
   valid commit in that session. If no rejection occurs, report 'not exercised', not successful recovery.
4. No claim of superiority over central scheduling without higher completion or a separately measured capability.
   A tie is a tie. API-enforced preservation is not proof that a model reasons about other goals.
5. Errors or incomplete sessions remain in denominators. An infrastructure failure may stop collection with a
   partial report; it does not authorize replacement runs or altered budgets. No tuning after outcomes appear.

## Freeze and next work

Before first model call, create a new results directory with exact protocol copy, immutable plan, prompts/tools,
local model digests, source SHA256 hashes and timestamp. Save each completed batch immediately; refuse to overwrite
an existing output directory. This timestamped local freeze is not a public preregistration. Keep source fixed
until this plan finishes. Any deviation requires a separate note. No commit, push, publishing or paid calls.

Stop after the 48 planned batches. Report the comparison and limitations before proposing larger samples,
natural-language extraction, changing arrival populations, actual network transport or FieldDesk integration.
