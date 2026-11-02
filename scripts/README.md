# Scripts

Run these from the repository root. Frozen records cite several of these paths, so they stay where they are.

| Script | Purpose |
|---|---|
| `make_release.sh` | Package trial bundles for publication |
| `clean_clone_check.sh` | Reproduce published results from a fresh clone, without a model key |
| `tamper_demo.sh` | Show that verification refuses altered evidence (works on a copy) |
| `oracle_fixture_check.py` | Show the oracle catching a wrong but successful booking |
| `queue.sh`, `eval_campaign.sh` | Run trial lists; `eval_campaign.sh` adds a spend cap |
| `eval_specs.py`, `grid_manifests.py`, `write_freeze.py` | Generate the run list, gate grid and freeze record |
| `agent_v2_run.py`, `bug_hunt.py`, `guard_campaign.sh`, `make_agent_v2.py` | Hardened-agent study: original vs hardened agent, bug hunter, guard trials, task and tool generation |
| `goal_preservation_demo.py`, `goal_preservation_live.py`, `audit_goal_preservation_live.py` | Goal-preservation study: mechanics demo, frozen local-agent cohort, independent audit |
| `interface_competence.py` | Local check: can an agent tell an evaluation from a booking, with and without effect-status wording |
| `policy_probe.py` | Refusal probe: served agent policy, wait tool, continuation and pacing arms |
| `build_ui.py` | Generate `ui/index.html` from recorded bundles |
| `local_models/` | Local-model study: campaign runner and specs, smoke test, semantic cases, raw replay, tool-schema generator. Live runs use `ollama_relay_win.py`, which `harness/ollama.py` starts with the Windows Python |
