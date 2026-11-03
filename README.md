# Rebound

**AI agents fail quietly against real APIs. Rebound measures it, contains it, and recovers what the customer actually asked for.**

An agent says "Done!", the API returns 200 OK, and the booking is wrong: Muse Spark booked 10:00 for a customer who wrote "6 pm only". Only an independent check of the database noticed. Rebound has two parts, built on **Meta Muse Spark 1.3** and a small field-service scheduling API (**FieldDesk**):

1. **The study (Oct 27–Nov 2):** real Muse tool-using agents rescheduling appointments while the API's database link is slowed. Every outcome is judged in SQL, never by a model.
2. **Resolve (Nov 3):** a scheduling-recovery assistant. When an outage or changed availability makes a request impossible, the model turns the customer's words into typed, versioned constraints. Deterministic code books only what the customer authorized, asks before any trade-off, survives a worker crash without double-booking, and SQL confirms the result.

> Built for the Meta Global AI Developer Hackathon 2026 (Indie track). Every number below is a measured value from a retained run, failures included. Synthetic data and scripted customers throughout.

## Results at a glance

| Finding | Evidence |
|---|---|
| Muse agents sent **every** retry before Retry-After expired | 69/69, 74/74, 68/68 (10 s fault); 139/139 (20 s) |
| A runtime guard that waits out Retry-After fixes it at no cost in correctness | 0 early, 209/209 correct goals |
| An admission gate shortens recovery mostly by shaping ordinary traffic | recovery 20–22 s → 10 s; agents were only 0.4–1.9% of in-flight time, so **agents did not measurably delay recovery** (our original thesis; a null result) |
| Resolve on 24 frozen held-out conversations, Muse Spark | **23/24** correct outcomes, **1 unauthorized booking**, 34 model calls, $0.23 |
| Same model, one-shot extraction, no history, no questions | 15/24, 2 unauthorized: the gap is in history references, corrections and renewed permission |
| Capable form with correct entries (same execution path) | 24/24: when the intent is right, the deterministic path makes no errors |
| Recovery checks on the live stack | 9/9: paging past 50 results, 7 invalid-intent kinds, crashes at 3 points, a real lost acknowledgment, renewed permission, correction preserved, confirm-before-booking |

The owner's bar for Resolve was at least 22/24 and zero unauthorized bookings: the first part is met, the second is not (one case, H15, "same time of day as my current booking", was read as "morning"). Details: [`docs/resolve_results.md`](docs/resolve_results.md).

## Resolve

```text
customer text --> model (Muse): typed intent, each rule quoting the customer's words
              --> validator: real dates, possible windows, technicians named or grounded in history
              --> versioned store (approved by a customer turn)
              --> worker: reconcile any unconfirmed write, search every open slot, decide
                     book (authorized) | ask permission (real slots) | clarify | nothing fits
              --> FieldDesk API (idempotency key per request and version)
              --> independent SQL oracle vs. hand-written expected outcomes
```

The model never sees availability, so it cannot invent slots; it never books, so it cannot book without authority. A correction is applied as explicit operations on the stored intent, so untouched constraints survive by construction. In product mode the customer confirms the exact slot before anything is written. Design, failure handling and security: [`docs/architecture.md`](docs/architecture.md) and the [decision records](docs/decisions/).

```bash
make sut-up && make seed      # stack and template data (see Quick start)
make resolve-ui               # the screen: http://127.0.0.1:8090/
make resolve-demo             # full story on the local model (Ollama); free
scripts/resolve_demo_muse.sh  # same on Muse Spark (paid, capped at 6 calls / $0.25)
make resolve-faults           # recovery checks on the live stack, no model
make resolve-form             # held-out cases through the form arm, no model

# deployed: worker + service containers (service on 127.0.0.1:8091, no auth: localhost only)
docker compose -f docker/compose.yml --profile resolve up -d
uv run python scripts/resolve_service_e2e.py   # intake over HTTP -> worker -> confirm -> SQL verdict (1 Muse call)
```

### When not to use AI here

- **A customer who taps a link and picks a slot should get a slot picker.** It is faster, free and cannot misread anyone. Resolve is for requests that arrive as text (SMS replies, email, call notes) and carry conditions a picker cannot record.
- **Retries belong in transport middleware**, not in a model's `wait` tool. The study's `wait` tool was a probe of agent behavior; it costs a model call per resume.
- **The durable queue, idempotency and SQL checks are standard engineering.** The model's job is only the part code cannot do: turning words into constraints and corrections.

### What did not work

- The study's thesis, that agents delay API recovery, was not supported at our traffic mix.
- Llama 3.1 8B via Ollama completed 0 of 24 goal-preservation tasks (cause not isolated: model or serving format).
- Locally (qwen3-14b) Resolve scored 16/24 with 2 unauthorized bookings; the first prompt made 3 unauthorized bookings in 6 dev cases.
- No rule-parser baseline, no notification channel, no real users. See the limitations in [`docs/resolve_results.md`](docs/resolve_results.md).

## The study: what it does

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

## Study results

For the charts, download the repository and open [`ui/index.html`](ui/index.html) in a browser. It needs no server, and every number comes from the recorded trial bundles.

The raw bundles are in the [`evidence-v1` release](https://github.com/sasikiran1/rebound/releases/tag/evidence-v1), one archive per experiment. To re-derive a result with no model key:

```bash
tar -xf eval-v1.tar && mkdir -p runs/eval-v1
for f in eval-v1/tr_*.tgz; do tar -xzf "$f" -C runs/eval-v1; done
make verify RUN=runs/eval-v1
```

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
- **Agents ignore Retry-After, every time.** The scripted client sent 3 of 23 early. Agents were only 0.4–1.9% of the API's load during the fault, so this did not slow recovery; the goals it cost were the agents' own. With a runtime guard that waits out Retry-After (`--retry-after-guard`), no request went early and no goal was lost: 209 of 209 at 10 s and at 20 s. That missed one pre-registered bar (refusals fell to 53–65%, the bar was 50%) and did not improve recovery. See [`docs/agent_v2_protocol.md`](docs/agent_v2_protocol.md) and [`docs/results.md`](docs/results.md).
- **Why agents ignored it: they had no way to wait.** In a refusal probe (every goal refused twice, Retry-After 2 s, 30 goals per arm), Muse sent 60 of 60 requests early with the original tools, and 0 of 60 once the tool list included a neutral `wait` tool. Muse used it after every refusal, with no prompt change. A served agent policy (`/.well-known/agent-policy.json`, stated in each 503) added nothing beyond the tool, and without the tool it left 56 of 60 early. A pacing sweep shows that apparent compliance without a wait tool is just client latency: 100% early with no pause, 85% with a 1 s pause, 0% at 2 s. Under the real 20 s fault, the wait tool alone kept 208 of 209 goals (201 without it) with 1 of 61 requests early, and 61 refusals against the client guard's 113 (one trial). See [`docs/policy_probe_results.md`](docs/policy_probe_results.md).
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
| `resolve/` | Resolve product: intent contract, interpretation, evaluator, service, store, worker, FieldDesk client, screen |
| `resolve/eval/` | Resolve evaluation: case seeding, runner arms, SQL oracle, recovery checks, scripted demo |
| `data/resolve/` | Resolve cases: 6 dev, 24 frozen held-out, 4 fault fixtures |
| `backend/` | FieldDesk, the API under test, with the admission gate |
| `harness/` | Trial runner, agents, scripted clients, fault control |
| `oracle/` | SQL verdicts on business outcomes |
| `analysis/` | Recovery metrics, contracts and reports |
| `manifests/`, `prompts/`, `data/` | Experiment settings, agent prompt and tools, seeded data and task sets |
| `docker/` | Compose stack (Resolve containers under the `resolve` profile) (fixed CPU sets; live trials need at least 16 vCPUs, `make verify` does not) |
| `docs/` | [Guide to the documents](docs/README.md), starting with the frozen rules in [`docs/protocol.md`](docs/protocol.md) |
| `scripts/` | [Guide to the scripts](scripts/README.md) |

All code and experiments here were built during the hackathon, Oct 27 – Nov 4, 2026.

## License

MIT
