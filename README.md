# Rebound

**HTTP 200 proves the API accepted a request. It does not prove the customer authorized the result.**

Rebound keeps AI agents from booking what a customer did not ask for, even when the request is vague, the booking
system is failing, or a worker crashes mid-write. It is built on **Meta Muse Spark 1.3** and a small field-service
scheduling API built for this project, **FieldDesk**, and has two parts:

1. **The study (Oct 27 – Nov 2):** real Muse Spark agents rescheduling appointments while the API's database link is
   slowed. Every outcome is judged in SQL, never by a model.
2. **Resolve (Nov 3 – 4):** a scheduling-recovery service built on what the study found. The model turns a customer's
   words into typed constraints; deterministic code decides what may be booked, asks before anything the customer did
   not clearly allow, waits out outages and recovers from crashes without booking twice; SQL checks the result.

> Meta Global AI Developer Hackathon 2026, Indie track. Synthetic customers and data, one laptop. Every number below
> is a measured value from a retained run, failures included.

## The problem

In the study, Muse Spark booked 10:00 for a customer who wrote "6 pm only". The agent said "Done!", the API said
200 OK, and only an independent database check noticed. Under back-pressure the same agents retried before
Retry-After every time, because their tools gave them no way to wait: adding a neutral `wait` tool took early retries
from 60 of 60 to 0 of 60. The failures were in the interface between the model and the system, which is where Resolve
puts its guarantees.

## How Resolve works

```text
customer text --> Muse Spark: typed intent; every rule quotes the customer's words
              --> validator: real dates, possible windows, technicians named or grounded in booking history
              --> versioned store: only a customer turn approves a version
              --> leased worker: settle any write in doubt, search every open slot, decide
                     book (authorized) | ask permission (real slots) | clarify | nothing fits
              --> FieldDesk API: one write per approved version (idempotency key; superseded keys voided)
              --> independent SQL check against hand-written expected outcomes (evaluation only)
```

- **The model proposes, code decides.** The model never sees availability, so it cannot invent a slot, and it never
  books. Corrections are explicit operations on the stored intent, so untouched constraints survive.
- **Confirm before booking.** In product mode the customer sees the exact slot and must say yes before anything is
  written.
- **Failure handling.** Outages release the request with backoff (honoring Retry-After); a crash after a write is
  reconciled by resending the same key (`replayed`, no second write); a correction or decline takes effect only after
  any write in doubt is settled; customer emails and calendar updates are queued in the same transaction as the state
  change and retried.
- **Integrations.** Questions and outcomes go to the customer by SMTP; each booking is mirrored into the technician's
  calendar over CalDAV. Locally both are containers (a test inbox, a Radicale server Thunderbird can subscribe to).

Details: [architecture](docs/architecture.md), [decision records](docs/decisions/).

**When not to use AI here:** a customer who taps a link should get a slot picker. Resolve is for requests that arrive
as text (SMS replies, email, call notes) with conditions a picker cannot record.

## Use of Meta AI

| Model | Where | What it does |
|---|---|---|
| Muse Spark 1.3 (Meta Model API, Responses endpoint, tool calling) | `resolve/interpret.py`, `resolve/models.py` | Turns a customer message into a typed intent (one tool call, JSON schema, at most one repair turn) and turns a later message into explicit patch operations. Capped per process by calls and dollars. |
| Muse Spark 1.3 | `harness/agent.py` | The study's tool-using agents: reschedule appointments through FieldDesk's API under injected faults. |
| Llama 3.1 8B (open weights, local via Ollama) | `resolve/eval/run.py` | Compared in the same Resolve pipeline: 10/24, mostly refusals rather than wrong bookings. |

Development used a free local model (qwen3-14b via Ollama); every Muse number below is from a paid, capped run.

## Results

| Measure | Result | Evidence |
|---|---|---|
| Resolve, 24 held-out conversations frozen before any model saw them, Muse | 23/24 correct (19 bookings + 4 correct refusals), 1 unauthorized booking, $0.23 | [resolve_results](docs/resolve_results.md) |
| Same, customer confirms the exact slot first (simulated customer who reads every proposal) | 23/24, 0 unauthorized, $0.25 | same |
| Same model in one shot (no history, no questions) | 15/24, 2 unauthorized | same |
| Hand-written rule parser, frozen before the held-out set | 20/24, 2 unauthorized | same |
| Form with correct entries, same execution path | 24/24 | same |
| Targeted recovery checks on the live stack (one run each, not reliability trials) | 20/20 | same; `make resolve-faults` |
| Study: early retries without a way to wait / with a `wait` tool | 69/69, 74/74, 68/68 and 139/139 early / 0 of 60 | [results](docs/results.md), [policy probe](docs/policy_probe_results.md) |
| Study: admission gate, recovery after a 10 s fault | 20–22 s to 10 s (the metric's floor) | [results](docs/results.md) |
| Study thesis, "agents delay recovery" | not supported: agents were 0.4–1.9% of in-flight time | [limitations](docs/limitations.md) |

The pass mark set before the held-out run was at least 22/24 and zero unauthorized bookings. Without confirmation
Resolve met the first and not the second: case H15, "same time of day as my current booking", was read as "morning"
and quoted real words with the wrong meaning, which validation cannot catch. Confirmation is the guard. One-page
summary: [case study](docs/case_study.md).

## Run it

**Prerequisites:** Linux, or Windows 11 with WSL2 (keep the repository in the Linux filesystem); Docker with Compose;
[uv](https://docs.astral.sh/uv/); at least 8 logical CPUs (compose pins containers to CPUs 2–6). A Meta Model API key
only for the Muse paths.

```bash
git clone https://github.com/sasikiran1/rebound && cd rebound
cp .env.example .env              # add MODEL_API_KEY for the Muse paths
uv sync
make sut-up                       # PostgreSQL 16, Toxiproxy, FieldDesk
make seed                         # seeded synthetic template database
make preflight                    # fresh trial database, FieldDesk restarted, health checked
make resolve-up                   # calendar server and test inbox
make check                        # lint, unit tests, no artificial delays in the backend
```

**No model key needed:**

```bash
make resolve-faults               # 20 recovery checks against the live stack
make resolve-form                 # the 24 held-out cases through the form arm, judged in SQL
docker compose -f docker/compose.yml --profile resolve up -d   # deployed worker and service
uv run python scripts/resolve_service_e2e.py --form            # intake -> worker -> booking -> calendar -> SQL
```

**The live demo (Muse):**

```bash
make resolve-live MODEL=muse-spark-1.3    # capped at 20 calls / $0.45; open http://127.0.0.1:8092/
```

Click **Rebound**, then **▶ Play scenario**: a messy request, the booking system going down, the customer's first
choice taken during the outage, a question, a correction, a confirmation, and a worker killed right after its write,
ending in one correct change and the event in the technician's calendar. About 2 Muse calls ($0.02). **One-shot
baseline** runs the same model without history, questions or confirmation on the same world. Recording guide:
[recording_runbook](docs/recording_runbook.md). The launcher stops the `resolve-worker` container while the screen runs;
start it again with `docker compose -f docker/compose.yml --profile resolve start resolve-worker`.

**Reproduce the study without a key:** download a bundle from the
[`evidence-v1` release](https://github.com/sasikiran1/rebound/releases/tag/evidence-v1), then

```bash
tar -xf eval-v1.tar && mkdir -p runs/eval-v1
for f in eval-v1/tr_*.tgz; do tar -xzf "$f" -C runs/eval-v1; done
make verify RUN=runs/eval-v1      # checksums first (fails closed), then re-analysis vs. the saved verdict
```

Live study trials need at least 16 logical CPUs and a key; see [protocol](docs/protocol.md). The results page
[`ui/index.html`](ui/index.html) opens in a browser without a server.

## Limitations

- Synthetic data, scripted customers, hand-written cases; 24 held-out conversations is a prototype check, not a
  reliability estimate, and the author wrote the cases, which favors the rule parser.
- Zero unauthorized bookings with confirmation assumes a customer who reads each proposal.
- The recovery checks are targeted checks of the mechanism under the injected conditions.
- No authentication: every service binds to 127.0.0.1. A local test inbox and CalDAV server stand in for real
  providers; the calendar is a one-way mirror.
- The study covers one model, one workflow, one fault type and one backend; at its traffic mix agents did not
  measurably slow recovery. Full list: [limitations](docs/limitations.md).

## Repository

| Path | Contents |
|---|---|
| `resolve/` | Resolve: intent contract, interpretation, evaluator, conversation service, store, worker, notifications, calendar sync, HTTP service |
| `resolve/eval/` | Resolve evaluation: case seeding, arms, SQL oracle, recovery checks, scripted demo, live console and screen |
| `data/resolve/` | 6 dev cases, 24 frozen held-out cases, 4 fault fixtures |
| `backend/` | FieldDesk, the booking API, with the admission gate and key fencing |
| `harness/`, `oracle/`, `analysis/` | The study: trial runner and agents, SQL verdicts, recovery metrics and reports |
| `manifests/`, `prompts/`, `data/` | Experiment settings, prompts and tool schemas, seeded data and task sets |
| `docker/` | Compose stack; Resolve containers under the `resolve` profile |
| `ui/` | Results page, Resolve screen, live demo screen |
| `docs/` | [Guide to the documents](docs/README.md) |
| `scripts/` | [Guide to the scripts](scripts/README.md) |

Built during the hackathon, Oct 27 – Nov 4, 2026. MIT license.
