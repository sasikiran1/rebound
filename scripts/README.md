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
| `local_models/` | Local-model study: campaign runner and specs, smoke test, semantic cases, raw replay, tool-schema generator. Live runs need the Windows relay that `harness/ollama.py` starts, which is no longer in this repo |
