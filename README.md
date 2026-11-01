# Rebound

**An agent recovery gate: does your API come back after a hiccup when AI agents are the ones calling it?**

APIs are increasingly called by AI agents rather than by fixed scripts. Agents react to errors in ways nobody wrote down: they retry, switch to more expensive endpoints, broaden searches or start over. Rebound measures what that does to an API's recovery after a brief dependency fault. It also tests whether one server-side admission gate helps without throwing away useful work.

> Built for the Meta Global AI Developer Hackathon 2026 (Indie track), Oct 26 – Nov 4, 2026. The frozen evaluation (18 trials, all valid) ran on Oct 28. Results below are measured values only.

## What it does

1. Runs a population of real tool-using agents, built on **Meta Muse Spark 1.3**, doing a real business task: rescheduling field-service appointments on **FieldDesk**, a small backend built for this project.
2. While they work, adds 400 ms of latency to the API's database link for 10 seconds, using Toxiproxy.
3. Measures when the API returns to healthy service, using backend-side data and a recovery definition written down before evaluation.
4. Repeats the same goals at the same times with controls: a robust scripted client (paced like the agents, or unpaced), the same trial with no goal clients at all (background only), and an open-loop replay of a healthy reference agent trace (a traffic regression, not a causal counterfactual: it ignores responses).
5. Turns on an admission gate (a concurrency limit with a bounded queue and fast 503 + Retry-After) and checks, with fresh agents, whether recovery improves while correct work is preserved.
6. Checks a recovery contract per trial: correct business outcomes for every scheduled goal, and whether each client waited out Retry-After before its next request. Every verdict carries witness IDs that join the model call, the HTTP attempt and the backend record.

An independent SQL oracle checks every goal against the database. **No language model grades anything.**

## How it works

```text
Meta Model API (muse-spark-1.3)
          ^ agent model calls
Runner (Python) --HTTP--> FieldDesk API (FastAPI + gate) --SQL--> Toxiproxy --SQL--> PostgreSQL 16
   |  fault control (Toxiproxy API)                                               ^
   +-- oracle (read-only, bypasses the proxy) ----------------------------------- +
   +-- results: runs/<experiment>/<trial>/  (JSONL events, verdicts, report.html)
```

| Part | Tech |
|---|---|
| Backend under test | FastAPI, Uvicorn, asyncpg, PostgreSQL 16 |
| Fault injection | Toxiproxy (latency toxic on the DB link) |
| Agents | Meta Model API, Responses endpoint, tool calling |
| Runner, oracle, analysis | Python 3.12, httpx, pandas, matplotlib, Jinja2 |
| Packaging | Docker Compose, uv, Make |

## Quick start (Windows 11 + WSL2, or any Linux)

```bash
# inside WSL2 Ubuntu (keep the repo in the Linux filesystem, not /mnt/c)
git clone https://github.com/sasikiran1/rebound && cd rebound
uv sync
make sut-up          # postgres, toxiproxy, fielddesk
make seed tasks      # seeded synthetic data and task sets
make preflight

# reproduce published results without any model key: raw checksums first (fails closed),
# then re-analysis into <trial>/reanalysis/, compared with the saved verdict
make verify RUN=runs/<experiment>

# run a fresh trial (needs a Meta Model API key; costs real money)
export MODEL_API_KEY=...
make trial MANIFEST=manifests/frozen/eval_v1.yaml CONDITION=agent GATE=off REPEAT=1
```

## Results

Frozen evaluation `eval-v1`: Muse Spark 1.3 agents, task set B, three paired blocks plus one unfamiliar 20 s fault pair. Every trial, per block, is in [`docs/results.md`](docs/results.md).

| | Gate off | Gate on |
|---|---|---|
| Recovery time after a 10 s fault (T_rec, 3 blocks) | 22 / 21 / 20 s | 10 / 10 / 10 s (the metric's floor) |
| Lost ordinary-user requests after a 10 s fault | 7,882–8,417 | 5,011–5,176 |
| Correct agent goals of 209 (10 s fault) | 209 each | 208 each |
| Recovery after the unfamiliar 20 s fault (1 pair) | 34 s, `slow` | 10 s |
| Lost ordinary-user requests, 20 s fault | 19,491 | 10,212 |
| Correct agent goals of 209, 20 s fault | 209 | 201 |
| Agent requests sent before Retry-After expired | — | 100% (69/69, 74/74, 68/68; 139/139 at 20 s) |

- **A short fault does not need the gate:** both arms recover fast, so by the frozen rules the gate is not needed at 10 s. It still spares ordinary users about 3,000 failed requests for one lost goal.
- **A longer fault does, with a cost:** at 20 s the gate halves lost service and restores health at once, but agents lose 8 correct goals (beyond the predeclared limit of 3; one trial pair, weaker evidence).
- **Agents ignore Retry-After, every time.** The scripted client sent 3 of 23 early. Agents were only 0.4–1.9% of the API's load during the fault, so this did not slow recovery; it cost the agents' own goals. A runtime guard (`--retry-after-guard`) is implemented but has not been run in a trial.
- **Separate finding (local models, no fault):** when the tool hid valid afternoon slots behind a 4 KB result cap, one model booked 08:00 and 11:00 against "15:00 or later", and the original date-only oracle passed those bookings. A start-time filter fixed 8 of 8 such cases; the oracle now checks stated constraints. See [`docs/ollama_protocol.md`](docs/ollama_protocol.md) for the local-model rules.

## What this is not

- Not a load-testing platform, and not a general chaos tool.
- Not a benchmark of models. It covers one workflow, one prompt, one fault and one scaled-down backend.
- Not tested against anyone else's systems. Everything runs on hardware we own, with synthetic data.

## Limitations

One paid model, one workflow, one fault type, one gate setting, one deliberately constrained backend on one laptop; three paired blocks and one unfamiliar-fault pair. The recovery metric has a 10 s floor. Agents were a small share of load, so this does not show that agents delay recovery. A recorded agent transcript cannot show what a fresh agent would do; only fresh live-agent trials can. See [`docs/limitations.md`](docs/limitations.md).

## Repository

| Path | Contents |
|---|---|
| `backend/` | FieldDesk, the API under test, with the admission gate |
| `harness/` | Trial runner, agents, scripted clients, fault control |
| `oracle/` | SQL verdicts on business outcomes |
| `analysis/` | Recovery metrics, contracts and reports |
| `manifests/`, `prompts/`, `data/` | Experiment settings, agent prompt and tools, seeded data and task sets |
| `docker/` | Compose stack (fixed CPU sets; live trials need at least 16 vCPUs, `make verify` does not) |
| `docs/` | [Guide to the documents](docs/README.md), starting with the frozen rules in [`docs/protocol.md`](docs/protocol.md) |
| `scripts/` | [Guide to the scripts](scripts/README.md) |

All code and experiments here were built during the hackathon, Oct 27 – Nov 4, 2026.

## License

MIT
