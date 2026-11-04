# Documents

| Document | Purpose |
|---|---|
| [case_study.md](case_study.md) | One-page summary: problem, what was built, results, decisions, what did not work |
| [architecture.md](architecture.md), [decisions/](decisions/) | Resolve architecture: components, state ownership, lifecycle, failure handling, deployment, security; decision records |
| [resolve_protocol.md](resolve_protocol.md), [resolve_results.md](resolve_results.md) | Resolve: evaluation rules, held-out results for Muse and the local model, recovery checks, demo, spend and limitations |
| [protocol.md](protocol.md) | Frozen evaluation rules for eval-v1 |
| [results.md](results.md) | Every measurement, in the order it was made |
| [limitations.md](limitations.md) | What the results do and do not support |
| [FREEZE.md](FREEZE.md) | Freeze record for eval-v1 |
| [agent_v2_protocol.md](agent_v2_protocol.md) | Pass marks for the hardened-agent study, fixed before its paid runs |
| [demo.md](demo.md) | Three-minute demo script |
| [recording_runbook.md](recording_runbook.md) | How to record the live screen: setup, Story view and Play, manual walkthrough, fallbacks |
| [ollama_protocol.md](ollama_protocol.md), [OLLAMA_FREEZE.md](OLLAMA_FREEZE.md) | Rules and freeze record for the local-model study |
| [commit_map.txt](commit_map.txt) | Maps commit hashes recorded in trial bundles to current commits |
| [spec.md](spec.md) | Original design; where it disagrees with the code or `protocol.md`, those win |
| [goal_preservation.md](goal_preservation.md), [goal_preservation_results.md](goal_preservation_results.md) | Experimental finite-goal API, mechanics checks and limits |
| [goal_preservation_live_protocol.md](goal_preservation_live_protocol.md), [goal_preservation_live_results.md](goal_preservation_live_results.md) | Frozen local-agent feasibility cohort: all 96 sessions and limitations |
| [interface_competence_protocol.md](interface_competence_protocol.md), [interface_competence_results.md](interface_competence_results.md) | Can a local agent tell an evaluation from a booking? Effect-status wording versus the cohort's wording |
| [policy_probe_protocol.md](policy_probe_protocol.md), [policy_probe_results.md](policy_probe_results.md) | Refusal probe: a served agent policy, a wait tool, and a pacing sweep |

Frozen files keep their paths because the analyzer fingerprint and the freeze records refer to them.
