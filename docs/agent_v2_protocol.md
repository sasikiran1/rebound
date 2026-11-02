# Hardened-agent study (agent-v2)

Written before any paid run of this study, on top of the frozen eval-v1 results, which it does not change. It asks
whether three changes make the agent more useful: (A) requests in plain language, with requirements the model records
and code enforces; (B) a runtime guard that waits out Retry-After; (C) a generator that hunts for failures. This is a
second, separate study on Muse Spark 1.3. Its code differs from eval-v1 (merged analyzer, adapter changes), so
comparisons with eval-v1 trials are between configurations and are labeled so.

## Spend

At most $22 more: cumulative recorded spend never above $55.85. Each runner prints an estimate first and stops at its
cap: A $4, C $3, B about $9.40 (four trials at about $2.35; the runner re-sums recorded spend and skips a trial that
could pass $55.85 at $2.60).

## A. Plain-language requests (`scripts/agent_v2_run.py`)

- Tasks: `data/tasks/tasks_messy_v1.jsonl`, 36 messages from task set B; message wording is hand-written. The 18
  `held_out` tasks were not run on any model before this protocol. The 18 `dev` tasks shaped the design on local gemma4.
- Arms: `original` (prompts/agent_v1.md, tools_v1) and `hardened` (prompts/agent_v2.md, tools_v3: start-time filters
  plus record_requirements, enforced by code before any booking). Same tasks, fresh database per arm, no fault, gate
  off, full_history, 16 model calls, 14 tool calls, 240 s.
- The oracle checks each task's hidden structured constraint. The agent sees only the customer's message.
- Run once, on `held_out` only. Reported: correct, incorrect, failed and wrongly deferred for each arm and task kind.
- Supported if hardened correct ≥ original correct + 4 of 18, and hardened incorrect ≤ original incorrect.
- Known limit, seen on local gemma4: code enforces what the model records, so a wrong extraction is not caught.

## B. Retry-After guard (`--retry-after-guard`)

- Trials: `manifests/frozen/eval_v1.yaml`, gate on, guard on: three at 10 s (blocks 1-3), one at 20 s.
  Comparators: eval-v1 tr_0004, tr_0010, tr_0016 and tr_0018 (gate on, no guard). Different code, same manifest.
- Supported if, across the three 10 s trials: requests sent before Retry-After expired = 0; refused goal requests per
  trial at most half of the comparator's; correct goals ≥ comparator − 1 in each pair. At 20 s the correct-goal
  change from 201 is reported, and is one pair, so weaker evidence.
- If the guard loses goals by waiting past deadlines, that is the result.

## C. Bug hunter (`scripts/bug_hunt.py`)

- 24 generated scenarios against the hardened agent, on base tasks not used in A, seed 20261102. SQL decides
  feasibility and the oracle gives the verdict; the model decides neither.
- Every non-correct verdict is a candidate. Each is classified by reading the message against the generator's
  constraint: `real` (the agent got it wrong), `generator_mismatch` (the message does not say what the constraint
  says) or `ambiguous`. Counts of all three are reported. The method is descriptive: there is no pass threshold.

## Rules

Every run is kept. A provider failure ending more than 5% of goals makes a run inconclusive. No threshold, prompt or
tool changes once the first paid run starts; any change means a new labeled run. Nothing here alters eval-v1.
