# Screening v2 plan (predeclared 2026-10-28, before any screening trial)

Exploration on task set A. Its job is to decide, within 24 elapsed hours of experimental work, whether the
back-pressure signature seen in explore-v1 tr_0014 survives the corrected agent context and fair controls. It is not
the confirmatory evaluation; that is frozen afterwards on task set B. Thresholds below were written before any trial
of this plan ran. Changing one is a new plan version.

## What explore-v1 showed (exploratory, one trial per condition, mixed configurations)

| Trial | Client | Continuation | Retry-After signals answered too early | Median gap after a 503 | Goals correct |
|---|---|---|---|---|---|
| tr_0011–tr_0013 | agent, gate on | previous_response_id (model call p50 ≈ 9.7 s) | 0 of 50 | 8.6–9.0 s | 210–214 of 228 (inconclusive: provider failures) |
| tr_0014 | agent, gate on | full_history (p50 0.75 s; reasoning dropped) | **84 of 84** | 0.77 s | 227 of 228 |
| tr_0004–tr_0007 | scripted v1, gate on | – | 0 of 97 | 2.0 s (honors Retry-After) | 223 of 228 each |

Witness in tr_0014: goal g_0083 (feasible) received 12 gate refusals in 9.4 s, each followed by the model's next call
0.6–1.2 s later; it called the expensive `search_availability` five times (four refused). Its first success came on
tool call 14 of 14 and the goal ended `limit_reached` with no change (oracle `failed`). Goal g_0078 had its reschedule
refused 11 times in a row and succeeded on its 14th and last allowed tool call. Slow agents complied only because each model call
took longer than Retry-After.

Confounds that make this exploratory: tr_0014 also dropped the model's reasoning context, differs from tr_0011–13 in
continuation and provider latency, and is one trial.

## Hypotheses and how each is falsified

- **H1, back-pressure violation.** With the gate on and the corrected `stateless_reasoning` context, live agents
  answer Retry-After too early in at least **50%** of refusals followed by a request, in **both** blocks, while the
  robust scripted client (paced and unpaced) and agents with the runtime guard show **zero** violations.
  *Falsified* if any block's agent share is below 50% (the tr_0014 behavior was an artifact of lost context or chance).
- **H2, the runtime guard preserves useful work.** Agents with `--retry-after-guard` versus without, per block:
  (a) refused goal requests per fault-cohort goal at most **50%** of the unguarded value; (b) correct goals at least
  unguarded minus **3**, with zero incorrect or duplicate effects; (c) F(60 s) at least unguarded minus **0.05**.
  *Falsified* if any block fails (a), (b) or (c).
- **H3, shared service (expected null at this load).** Agents versus the robust paced script on T_rec, lost
  background service D and correct goals, using the contract comparison (margins +5 s, +500, 3 goals; must exceed the
  control spread in both blocks). Predicted NO_MATERIAL_ADVERSE_EFFECT, because goal clients held 0–1.7% of in-flight
  time during the fault in every explore-v1 trial. A material effect here would be a surprise and needs held-out
  confirmation before any claim.

Every scheduled goal, failure, rejection, provider error and invalid trial is counted. Verdicts come from
`python -m analysis.screening runs/screen-v2` on re-analyzed bundles.

## Arms (manifest `manifests/exploratory/screen_v2.yaml`, gate on in every arm)

| Arm | Command spec for scripts/queue.sh | Purpose |
|---|---|---|
| Healthy reference | `agent on --no-fault --block B --label bB-healthy-reference` | false-positive control; block 1's trace paces the scripted arm |
| Agent | `agent on --block B --label bB-agent` | subject |
| Agent + guard | `agent on --retry-after-guard --block B --label bB-agent-guard` | intervention |
| Robust script, paced | `scripted on --block B --label bB-scripted-paced` | strong non-LLM baseline, agent step-gap distribution |
| Robust script, unpaced | `scripted on --unpaced --block B --label bB-scripted-unpaced` | practical baseline |
| Background only | `background on --block B --label bB-background-only` | attribution: the fault and gate without goal clients |

Order: the healthy reference first in each block (block 1's is trial tr_0001, which the paced arm reads), then the
seeded shuffle (seed 20261028): block 1 agent-guard, scripted-unpaced, scripted-paced, agent, background;
block 2 agent-guard, background, scripted-paced, scripted-unpaced, agent.

## Forecast

- 12 trials × about 13 min (≤ 790 s trial + reset + 60 s cool-down) ≈ **2.6 h unattended**.
- 6 agent trials × about $2.5–3.0 (tr_0014: $2.50; encrypted reasoning adds mostly cached input) ≈ **$15–18**,
  plus about $0.02 of smoke checks. Per-trial cap stays $10.

## Stop rules

- Smoke checks first: `make verify-model CONTINUATION=stateless_reasoning` must show encrypted reasoning returned and
  accepted back; `make goal TASK=A0001 CONTINUATION=stateless_reasoning` must end `correct`. If the provider rejects
  the replayed items, spend at most 1 focused hour, then fall back to `full_history` **labeled as a different
  configuration** and drop any claim about reasoning context.
- If block 1 is invalid for infrastructure reasons, fix setup and rerun the block; more than two invalid blocks means
  simplify the setup and narrow the claim.
- **Decision by 24 elapsed hours from the first screening trial.** H1 and H2 SUPPORTED: freeze protocol v2 on task
  set B (contract and these arms) and run the confirmatory blocks. H1 NOT_SUPPORTED: stop the counterexample branch
  and complete the honest bounded benchmark (gate effect with background-only attribution; contract reported as a
  measurement). Do not sweep load or fault settings looking for an effect.
