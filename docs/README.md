# Documents

Start with the [case study](case_study.md) (one page), then [architecture](architecture.md) and the
[Resolve results](resolve_results.md).

**Resolve (the product)**

| Document | Purpose |
|---|---|
| [case_study.md](case_study.md) | One-page summary: problem, what was built, results, decisions, what did not work |
| [architecture.md](architecture.md), [decisions/](decisions/) | Components, state ownership, request lifecycle, failure handling, deployment, security; decision records 0001–0008 |
| [resolve_protocol.md](resolve_protocol.md), [resolve_results.md](resolve_results.md) | Evaluation rules; held-out results for Muse, the rule parser and local models; recovery checks; spend and limitations |

**The study (Oct 27 – Nov 2)**

| Document | Purpose |
|---|---|
| [protocol.md](protocol.md) | Frozen evaluation rules for eval-v1 (freeze record with the freeze commit and trial-order seed: `docs/FREEZE.md` in commit `1f22c9b`) |
| [results.md](results.md) | Every measurement, in the order it was made |
| [limitations.md](limitations.md) | What the results do and do not support |
| [agent_v2_protocol.md](agent_v2_protocol.md) | Pass marks for the hardened-agent study, fixed before its paid runs |
| [policy_probe_protocol.md](policy_probe_protocol.md), [policy_probe_results.md](policy_probe_results.md) | Refusal probe: a served agent policy, a wait tool, and a pacing sweep |
| [ollama_protocol.md](ollama_protocol.md) | Rules for the local-model study (freeze record: `docs/OLLAMA_FREEZE.md` in commit `1b676b3`) |
| [goal_preservation.md](goal_preservation.md), [goal_preservation_results.md](goal_preservation_results.md) | Experimental finite-goal API, mechanics checks and limits |
| [goal_preservation_live_protocol.md](goal_preservation_live_protocol.md), [goal_preservation_live_results.md](goal_preservation_live_results.md) | Frozen local-agent feasibility cohort: all 96 sessions and limitations |
| [interface_competence_protocol.md](interface_competence_protocol.md), [interface_competence_results.md](interface_competence_results.md) | Can a local agent tell an evaluation from a booking? Effect-status wording versus the cohort's wording |
| [spec.md](spec.md) | Original design; where it disagrees with the code or `protocol.md`, those win |

Frozen files keep their paths because the analyzer fingerprint refers to them. The freeze records and the map from commit hashes in trial bundles to current commits (`docs/commit_map.txt`, commit `63c39e7`) are in git history: `git show <commit>:<path>`.
