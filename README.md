# Rebound

**An agent recovery gate: does your API come back after a hiccup when AI agents are the ones calling it?**

APIs are increasingly called by AI agents rather than by fixed scripts. Agents react to errors in ways nobody wrote down: they retry, switch to more expensive endpoints, broaden searches or start over. Rebound measures what that does to an API's recovery after a brief dependency fault. It also tests whether one server-side admission gate helps without throwing away useful work.

> Status: in development for the Meta Global AI Developer Hackathon 2026 (Indie track), Oct 26 – Nov 4, 2026. Results below are filled in from measured trials only.

## What it does

1. Runs a population of real tool-using agents, built on **Meta Muse Spark 1.3**, doing a real business task: rescheduling field-service appointments on **FieldDesk**, a small backend built for this project.
2. While they work, adds 400 ms of latency to the API's database link for 10 seconds, using Toxiproxy.
3. Measures when the API returns to healthy service, using backend-side data and a recovery definition written down before evaluation.
4. Repeats the same goals at the same times with a careful scripted client and with a blind replay of recorded agent traffic.
5. Turns on an admission gate (a concurrency limit with a bounded queue and fast 503 + Retry-After) and checks, with fresh agents, whether recovery improves while correct work is preserved.

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
git clone https://github.com/<you>/rebound && cd rebound
uv sync
make sut-up          # postgres, toxiproxy, fielddesk
make seed tasks      # seeded synthetic data and task sets
make preflight

# reproduce published results without any model key
make analyze RUN=runs/<experiment>/<trial>

# run a fresh trial (needs a Meta Model API key; costs real money)
export MODEL_API_KEY=...
make trial MANIFEST=manifests/frozen/eval_v1.yaml CONDITION=agent GATE=off REPEAT=1
```

## Results

*To be measured.* Every evaluation trial, including invalid ones, will be listed in [`docs/results.md`](docs/results.md), with per-trial values rather than best runs.

## What this is not

- Not a load-testing platform, and not a general chaos tool.
- Not a benchmark of models. It covers one workflow, one prompt, one fault and one scaled-down backend.
- Not tested against anyone else's systems. Everything runs on hardware we own, with synthetic data.

## Limitations

One model (an optional second Meta model if budget allows), one workflow, one fault type, one deliberately constrained backend on one laptop. A recorded agent transcript cannot show what a fresh agent would do; only fresh live-agent trials can. See [`docs/limitations.md`](docs/limitations.md).

## Repository

See [`docs/spec.md`] for the build rules and [`docs/spec.md`](docs/spec.md) for the full design specification.

## License

MIT
