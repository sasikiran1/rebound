# Rebound engineering decision, 2026-10-28

Inputs: the external audit of HEAD 5694a04 (2026-10-27), the repository at that commit, and the saved explore-v1
bundles. Work happened on branch `audit-fixes` (8 commits on top of 5694a04). No model call was made, no trial was
run, nothing was published. As of 2026-10-27 23:55 EDT the submission deadline (Nov 4, 9:00 p.m. EST = Nov 5 02:00
UTC; daylight saving ends Nov 1) is about **190 hours** away.

**Decision: NARROW.** Keep the bench. Stop claiming or hunting for "agents prolong the outage": at this load goal
clients hold under 2% of the API's in-flight time during the fault, so they cannot move recovery. Make the submission
about one checkable contract: **does a live Meta agent honor the server's back-pressure signal while still finishing
correct business work, and does one runtime intervention fix it without losing work.** The exploratory evidence for a
violation is strong but comes from one trial under the old, context-dropping mode. A predeclared, 2.6-hour,
roughly $17 screening (`docs/screening_v2.md`) confirms or kills it before anything is frozen.

---

## 1. Audit findings, verified against the code and the saved bundles

Baseline captured before any edit: 77 tests passed, ruff clean, no `sleep` in `backend/`, clean tree, 21 commits,
HEAD 5694a04. The six integrity regression tests below were written first and **failed on 5694a04**.

| # | Finding | Status | Evidence (5694a04) | Consequence | Change made |
|---|---|---|---|---|---|
| 1 | Continuation loses reasoning and typed items | **Confirmed** | `harness/agent.py` full_history branch appends only visible text and function calls; no `include`, reasoning items dropped | tr_0014's fast agents ran without their own earlier reasoning; the latency and behavior change cannot be attributed to speed alone | New `stateless_reasoning`: store=False, `include=["reasoning.encrypted_content"]`, every output item resent verbatim and in order, pairing checked before each call; full_history kept as a labeled historical mode. 6 tests. **Live check still needed.** |
| 2 | Summary pools incompatible trials | **Confirmed, with consequence** | `analysis/experiment.py` `row()` has no config hash; cells grouped by condition/gate/fault only | On the saved verdicts the old summary pooled four different gate settings (tr_0004–07) into one scripted gate-on cell, pooled tr_0001 and tr_0003 (different think times), and paired tr_0002 (stored, gate off) with tr_0014 (full_history, gate on) as an acceptance pair that "passed" c3 and c4. The 9 valid explore-v1 trials span **8 configurations** | Cells keyed by compatibility fingerprint; mixed fingerprints or analyzers are reported and not judged; duplicate arms in a block are excluded and named |
| 3 | Checksum failure does not invalidate | **Confirmed** | `harness/__main__.py` `cmd_analyze` printed "CHECKSUM MISMATCH" and analyzed anyway, overwriting derived files | A corrupt bundle could produce a fresh valid-looking report | Raw evidence is verified first; any mismatch, missing SUMS or unlisted raw file means nothing is analyzed and exit 1. Re-analysis writes to `<trial>/reanalysis/`, never over recorded files. Demonstrated: one word flipped in tr_0002's `oracle.jsonl` gives INTEGRITY FAILURE |
| 4 | Provenance omits decision-making code | **Confirmed** | `config_hash` covered manifest, prompt, tools, images, data, tasks, harness and backend code only | Changing the oracle or schema left the configuration identity unchanged | config_hash v2 adds oracle, schema, indexes, generator, uv.lock, docker; `analysis_fingerprint` (analysis + protocol) recorded per verdict; replay trace hash and pacing-sample hash recorded |
| 5 | Undefined acceptance criteria | **Confirmed** | c1 used `abs(x or 0)`, so missing baselines passed; c2 `None` with < 3 pairs; no c5; `.iloc[0]` hid duplicates | A gate could "pass" on missing evidence | Five criteria, each pass / fail / insufficient_evidence, plus overall; missing or non-finite inputs never pass; c5 from the unfamiliar fault |
| 6 | Insert time presented as commit | **Confirmed, bounded** | `applied_at DEFAULT clock_timestamp()` (schema.sql:74) is the INSERT statement time; COMMIT follows on context exit (app.py) | Completion times early by one round trip: about 1 ms healthy, about 400 ms in the fault. Not a large error, but mislabeled | Oracle field renamed `change_inserted_at`; FieldDesk logs `commit_ack_mono_ns` + `change_id` when COMMIT returns (even if the client gave up); completion curves report their basis |
| 7 | Weak scripted baseline | **Confirmed** | `scripted.py` returns when the first read fails its 2 retries | 7 and 5 goals lost in tr_0001/tr_0003, all during the fault; v1 is not "careful" | `robust_v2`: retries until budget or deadline with capped full-jitter backoff and Retry-After, never abandons the first read, reads back after an uncertain write, budget counted per HTTP attempt; paced (fixed or recorded step-gap distribution) and unpaced. v1 unchanged |
| 8 | Replay overclaimed | **Confirmed** | README "blind replay"; explore_replay replays faulted tr_0002; replay never checks its source | tr_0008 reproduces tr_0002's fault reactions (4.45 attempts per fault goal, identical); it is a traffic regression, not a counterfactual | Preflight checks the reference (live agent, no fault, same tasks and seed, intact sums); explore_replay labeled `replay_mechanics_only`; README reworded |
| 9 | Agents barely load the API | **Confirmed in the fault window itself** | New overlap measurement over 13 bundles | Goal clients held 0–1.7% of in-flight time and 0–2.7% of DB round-trip ms during the fault; background held ≥ 98% | Exposure shares reported per trial; background-only arm added; no shared-service claim without it |
| 10 | DB elapsed ≠ DB work | **Confirmed** | db.py times round trips through Toxiproxy | "DB ms per goal" is mostly injected latency during the fault | Relabeled "DB round-trip elapsed ms (includes proxy latency; not DB CPU)" everywhere |

Audit statements corrected: finding 6's error is bounded to one round trip (clock_timestamp, not transaction start).
The Contributor-tier rate limit is 150 RPM in docs/spec.md and 100 RPM in LiteLLM's launch note; unresolved and
irrelevant, since that tier is not used. The audit's own recomputation is confirmed and extended: **13 of 14 bundles
pass raw checksums and reproduce every saved version 1 result exactly**; tr_0010 (runner crash, already INVALID) has
a truncated last line and cannot be analyzed.

### New exploratory finding (not in the audit)

| Trials | Client | Requests sent before Retry-After expired | Median gap after a 503 |
|---|---|---|---|
| tr_0004–tr_0007 | scripted v1, gate on | 0 of 97 | 2.0 s |
| tr_0011–tr_0013 | agent, previous_response_id (model call p50 ≈ 9.7 s) | 0 of 50 | 8.6–9.0 s |
| tr_0014 | agent, full_history (p50 0.75 s) | **84 of 84** (10 goals) | 0.77 s |

Witness, tr_0014 goal g_0083 (feasible): 12 gate refusals in 9.4 s, each answered by the model's next call 0.6–1.2 s
later with messages such as "Transient errors — I'll keep retrying"; five calls to the expensive broad search (four
refused). Its first success came on tool call 14 of 14, so it ended `limit_reached` and the oracle says `failed`.
g_0078 had its reschedule refused 11 times and succeeded on its last allowed tool call. Slow agents "complied" only
because each model call outlasted Retry-After. The tool interface offers no way to wait.

## 2. Direction: NARROW

KEEP the bench (FieldDesk, Toxiproxy, oracle, adapter, gate). NARROW the claim from "agents delay recovery" (untestable
at under 2% occupancy, and not supported) to a back-pressure and useful-work contract. A PIVOT is not justified: the
bench already measures exactly what the new claim needs. TRANSFORM (an autonomous fault hunter) would cost the
remaining window and face AgentCheck, MCP-Injector and stateful fuzzers.

## 3. The contribution and how it can be disproved

**Claim (to be confirmed):** a live Muse Spark 1.3 tool agent that passes healthy business checks sends requests
before the server's Retry-After expires once its model calls are faster than Retry-After, and on a real fault this
burns its tool budget and can lose correct work. A runtime Retry-After guard removes the violations without losing
correct goals. Rebound detects this from joined, checksummed evidence and re-verifies it without a model key.

| Question | Answer |
|---|---|
| Who needs it | API/platform engineers putting agent-facing transactional APIs behind rate limits or admission control; agent engineers choosing runtime and continuation settings |
| What existing workflow misses | Healthy task evals pass (39/39 baseline-cohort goals correct in tr_0014). Load tests use scripted clients that honor backoff. HTTP-level Retry-After handling in SDKs covers transport retries, but model-initiated retries arrive as new tool calls and bypass it |
| What the model decides | After a tool result carrying `retry_after_s: 2`, the next action: retry the same call, escalate to the expensive search, re-read, defer, or stop |
| Independently measured consequence | FieldDesk's own request log (gate decisions), the SQL oracle (business outcome), per-goal tool budget use, joined by attempt IDs |
| Intervention | Runtime guard (`--retry-after-guard`): holds the goal's next request until Retry-After passes; the model's choices are unchanged |
| Why alternatives do not already give this | AgentCheck injects tool-response faults and checks handling rules, but not against a real admission gate under a real dependency fault, with shared traffic and SQL business truth. τ²-bench checks end state, not back-pressure. Load and chaos tools have no live model. This is a narrow, packaged, verifiable test, not a new primitive |
| What disproves it | (a) With `stateless_reasoning`, agent violation share is below 50% in either screening block (tr_0014 was an artifact). (b) The guard fails to halve refused requests per fault-cohort goal, loses more than 3 correct goals, or lowers F(60 s) by more than 0.05. (c) Held-out task set B does not repeat (a) and (b) |

The shared-service consequence (recovery time, lost background service) is reported as H3 with a predicted null. It
is not part of the claim unless it beats the declared margins and the control spread.

## 4. KEEP / MODIFY / REMOVE / BUILD

| Decision | Item | State |
|---|---|---|
| KEEP | FieldDesk, Postgres, Toxiproxy, generator, adapter scope and idempotency, oracle, gate, JSONL bundles, reports | unchanged except as below |
| MODIFY | agent continuation (stateless_reasoning) | done, needs live smoke |
| MODIFY | provenance (config_hash v2, compat and analyzer fingerprints) | done |
| MODIFY | analyze CLI: fail closed, separate output, compare with saved | done (`make verify`) |
| MODIFY | experiment summary and acceptance verdicts | done |
| MODIFY | scripted baseline (robust_v2, paced/unpaced) | done |
| MODIFY | completion timing (commit ack) and DB-time labels | done; needs a fielddesk image rebuild |
| MODIFY | Contributor-tier price | done ($0.10 / $0.002 / $0.20) |
| REMOVE from claims | "agents prolong the outage", "blind replay isolates reaction", "applied_at = commit time", any platform / OpenAPI / diagnosis / second-model scope | done in README, docs/spec.md, protocol draft |
| BUILT | `analysis/contracts.py` (per-trial contract, paired comparison), back-pressure and exposure metrics, background-only arm, runtime guard, replay provenance check, `analysis/screening.py`, `docs/screening_v2.md` | done |
| BUILD next | frozen protocol v2 + FREEZE.md (after screening) | Oct 29 |
| BUILD if the effect repeats | 2-minute contract regression: about 20 goals through a forced 10 s refusal window, no DB fault, about $0.25 per run | Oct 31, 4 h cap |
| BUILD if time | `wait_seconds` tool arm (does Spark choose to wait when it can?) as a separately versioned tool set | Nov 1, only after the core result |
| DEFER | second workflow, LLM scenario search, ambiguous-write pivot, Wonder cohort | – |

## 5. Implementation and verification results

| Check | Result |
|---|---|
| Test suite | **115 passed** (38 new: integrity 7, context replay 6, back-pressure / guard / robust script 7, contract 11, trial controls 7) |
| Lint, no-sleep | ruff clean; no `sleep` in `backend/` |
| New tests on old code | 6 integrity tests failed on 5694a04 (pooled cells, missing-baseline pass, hidden duplicates, no c5, oracle change not in identity, analysis on corrupt evidence) |
| Key-free re-analysis | 13/13 analyzable bundles: raw sums verified, every version 1 value identical to the saved verdict |
| Tamper test | one word changed in `oracle.jsonl`: INTEGRITY FAILURE, exit 1, no output written |
| Contract on saved bundles | scripted gate-on tr_0004–07: PASS; agent tr_0014: FAIL (back-pressure, 84/84, witnesses g_0001, g_0078, …); gate-off trials: INSUFFICIENT_EVIDENCE (back-pressure not exercised); invalid or inconclusive trials: INSUFFICIENT_EVIDENCE |
| Not verified here | Live Meta calls (stateless mode), a fresh trial, the fielddesk image rebuild, Docker integration, actual billing |

## 6. Baselines, metrics, acceptance and negative controls

Screening arms (task set A, gate on everywhere so the signal is exercised): healthy agent reference, agent, agent +
guard, robust script paced from the reference's step-gap distribution, robust script unpaced, background only. Same
fixture, tasks, arrival seed, fault and budgets. Every scheduled goal counted; provider errors, refusals and invalid
trials kept.

| Metric | Definition | Acceptance (predeclared) |
|---|---|---|
| Back-pressure violation share | requests sent earlier than Retry-After − 50 ms after a 429/503 with Retry-After, ÷ such refusals followed by a request | H1: agent ≥ 0.5 in both blocks; scripts and guarded agents = 0 |
| Refused requests per fault-cohort goal | gate-rejected goal requests in [t_on, t_off + 60) ÷ fault-cohort goals (backend log) | H2a: guard ≤ 50% of unguarded, both blocks |
| Business correctness | oracle verdicts over all scheduled goals | H2b: guard correct ≥ unguarded − 3, zero incorrect or duplicate |
| F(60 s) | correct feasible goals completed within 60 s, commit-ack basis | H2c: guard ≥ unguarded − 0.05 |
| T_rec, D, correct vs robust paced script | contract `compare`, margins +5 s / +500 / 3 | H3: material only above margin and control spread in both blocks; predicted null |
| Exposure | goal vs background share of in-flight seconds and DB round-trip ms in the fault window | reported, explains H3 |

Negative controls: the healthy reference must not FAIL the contract; the guarded agent and both scripts must show 0
violations (an implementation check); a corrupted bundle must never PASS (tested); a known violation must be
detected (tested); a single block is never enough (tested).

## 7. Plan

### Next 24 hours (Tue Oct 27 night → Wed Oct 28)

| When | Step | Done when | If not |
|---|---|---|---|
| Tonight, 15 min | Fetch branch into `~/rebound`, `make check`, `make sut-up` (rebuilds fielddesk) | 115 passed; preflight OK | fix setup; no trials |
| Tonight, 10 min, about $0.02 | `make verify-model CONTINUATION=stateless_reasoning`; `make goal TASK=A0001 CONTINUATION=stateless_reasoning` | encrypted reasoning returned and accepted; oracle `correct` | ≤ 1 h on provider; then `full_history` labeled as a separate config, no reasoning-context claim |
| Overnight, unattended, about 80 min, about $8.5 | Screening block 1 (6 trials) via scripts/queue.sh | 6 bundles, valid or invalid with reasons | invalid infra: fix, rerun block |
| Wed morning, about 80 min, about $8.5 | Screening block 2 | 12 bundles | – |
| Wed midday | `make verify RUN=runs/screen-v2`; `python -m analysis.screening runs/screen-v2` | H1, H2, H3 verdicts | – |
| Wed afternoon | Decision (deadline: 24 h after the first screening trial) | H1 + H2 SUPPORTED → write protocol v2; otherwise stop the counterexample branch, honest benchmark | no sweeps |

### Remaining days

| Date (ET) | Work | Deliverable | Fallback |
|---|---|---|---|
| Thu Oct 29 | Write and freeze protocol v2 on task set B (contract, arms, thresholds, seeds, inventory); FREEZE.md commit; healthy reference + block 1 | freeze hash before the first evaluation trial | null path: freeze the benchmark protocol instead |
| Fri Oct 30 | Blocks 2–3 (agent, agent+guard, robust paced, robust unpaced, background; gate on) | 3 complete blocks; about $9 per block | keep invalid trials, repeat a block once |
| Sat Oct 31 | Held-out 20 s fault pairs (guard off/on); `make verify`; results with all trials | effect sizes and spread per block | report insufficient cells as such |
| Sun Nov 1 (EST from today) | If repeated: 2-minute contract regression (4 h cap); optional `wait_seconds` arm | short live demo test | skip; use recorded evidence |
| Mon Nov 2 | Results write-up, limitations, README; **feature freeze tonight** | source-linked results, budget ledger vs billing | narrow wording |
| Tue Nov 3 | Fresh-clone, key-free reproduction; demo recording and rehearsal | timed 3-minute video | labeled recorded segments only |
| Wed Nov 4 | Repo public check, video link, submission by 3 p.m. ET (6 h buffer before 9 p.m.) | submitted | packaging fixes only |

Budget: about $14 estimated spent; screening about $17, evaluation about $35–40, regression runs about $2. About $55–60
planned, with room under the $150 credit. Compare with the billing page before stating exact spend.

## 8. Three-minute demonstration

| Time | Shows | Evidence on screen |
|---|---|---|
| 0:00–0:15 | "Agents get faster. Does your API's back-pressure still work?" One scheduling task, the contract | goal, contract predicates |
| 0:15–0:35 | Live: one fresh `muse-spark-1.3` goal completes; SQL oracle `correct` | response IDs, tool calls, oracle row |
| 0:35–1:05 | Labeled playback of a frozen trial: fault, gate 503 + Retry-After: 2; witness goal's model messages and requests every ~0.8 s; tool budget runs out; oracle `failed` | joined model call → attempt → backend rows |
| 1:05–1:25 | Controls: robust script 0 violations; slow agents complied only by latency; background-only recovery identical, so no shared-service claim | per-arm table, exposure shares |
| 1:25–2:00 | One change: runtime guard on. Violations 0, refused requests halved or better, correct goals preserved (frozen numbers only) | paired blocks |
| 2:00–2:30 | `make verify` with no key: checksums, re-analysis matches, contract FAIL → PASS; tamper one byte → INTEGRITY FAILURE | terminal |
| 2:30–3:00 | Decision: "Don't ship a faster agent runtime without a Retry-After guard. Rebound gives you the test." Limits: one app, one fault | one sentence, limits |

Fallback: if the live call stalls, a timestamped recorded real call. If H1 fails in screening, the honest version:
"We tested whether this agent violates back-pressure under corrected context. It does not; here is the evidence that
stopped us from claiming otherwise," plus the integrity demo (refusing a tampered bundle). Never label playback live.

## 9. Evidence and reproduction plan

- **Deterministic analysis:** `make verify RUN=runs/<exp>` on a fresh clone, no model key, no containers. Raw
  checksums first (fails closed), re-analysis into `reanalysis/`, comparison with saved verdicts, `contract.json`.
- **Recorded-traffic reproduction:** replay of the healthy reference (traffic regression only; ignores responses).
- **Live confirmation:** fresh agent trials under the frozen manifest, reported separately with repeat rates.
- **Packaging:** each trial bundle as `tr_XXXX.tgz` (about 18 MB from about 370 MB) in a release archive with a
  top-level hash list; images pinned by digest; `uv.lock`; `docs/FREEZE.md` with the freeze commit; every invalid trial
  included with its reason.

## 10. Independent scores (not the official rubric)

Same seven-criterion rubric as the audit (20/15/15/15/15/10/10), for comparability. The official judging weights
are different (participant guide: Impact 30%, Use of Meta AI Tooling 25%, Technical Depth and Execution 20%, Demo
25%, after a pass/fail viability stage). These are reasoned judgments, not predictions.

| Criterion | Max | Audit (5694a04) | Now (branch, no new trials) | Achievable: H1+H2 confirmed on held-out B | Null path |
|---|---:|---:|---:|---:|---:|
| Originality | 20 | 10 | 11 | 15 | 11 |
| Problem importance | 15 | 11 | 11 | 12 | 11 |
| Meaningful AI engineering | 15 | 9 | 10 | 12 | 10 |
| Technical depth | 15 | 13 | 14 | 14 | 14 |
| Demo impact | 15 | 8 | 9 | 14 | 10 |
| Completion feasibility | 10 | 9 | 9 | 9 | 9 |
| Practical utility | 10 | 7 | 7 | 8 | 7 |
| **Total** | 100 | **67** | **71** | **84** (range 80–87) | **72** (range 70–78) |

Now is +4, not more: the integrity, provenance and baseline work is real but mostly prevents rejection; the
back-pressure finding is one exploratory trial under a mode now known to be flawed.

## 11. What still blocks a credible 95+

1. **"It's a runtime bug, not an AI result."** Any client without backoff ignores Retry-After. The defensible point is
   narrower: the model sees `retry_after_s`, chooses immediate retries and escalates to the expensive endpoint, and
   compliance flips with model speed. Healthy evals miss it. The guard is a conventional fix.
2. **One application, one fault, one gate.** No transfer evidence; a second workflow does not fit the window safely.
3. **Shared-service consequence is null at this load.** The harm shown is to the agent's own useful work and to the
   server's refused-request volume, not to ordinary users.
4. **Small n.** Three blocks of shared-system trials; no population-level rate.
5. **Close prior art.** AgentCheck-style fault and mitigation loops and τ²-bench state oracles cover much of the
   method; the contribution is the packaged, verified contract on a real backend.
6. **No user.** No external engineer has used it or changed a decision with it.

---

**What justifies the differentiation now:** a fail-closed evidence chain (13 bundles independently reproduced, tamper
refused) and one fully joined exploratory witness: a Meta agent that passed healthy checks retried 12 times in 9.4 s
through Retry-After, spent its whole tool budget and lost a correct goal, while the robust script never violated
Retry-After and slow agents complied only by accident. **What remains unproven:** that the behavior survives the
corrected reasoning context, repeats across blocks and held-out tasks, and that the guard fixes it without losing
work. **Single next action with the highest value:** run `make verify-model CONTINUATION=stateless_reasoning`, then
screening block 1 tonight. It costs about $8.5 and its result decides the whole submission.
