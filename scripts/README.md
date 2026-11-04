# Scripts

Run from the repository root.

| Script | Purpose |
|---|---|
| `resolve_live_ui.sh` | The live demo screen on port 8092 (also `make resolve-live`) |
| `resolve_live.sh` | The same session as a terminal console: type as the customer |
| `resolve_service_e2e.py` | Check the deployed containers over HTTP: intake, worker, booking, calendar, SQL verdict (`--form`: no model) |
| `resolve_demo_muse.sh` | Scripted end-to-end demonstration on Muse (at most 6 calls / $0.25) |
| `resolve_eval_local.sh`, `resolve_muse.sh` | Held-out evaluation on the local model, or on Muse one part at a time under a hard cap |
| `resolve_inspect.py` | Print a run's intents, validator problems, decisions and questions per case |
| `local_models/ollama_relay_win.py` | Relay to a local Ollama on the Windows host, started by `harness/ollama.py` |
