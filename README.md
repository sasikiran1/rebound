# Rebound

**HTTP 200 proves the API accepted a request. It does not prove the customer authorized the result.**

Rebound's product, **Resolve**, keeps an AI agent from booking what a customer did not ask for, even when the request
is vague, the booking system is failing, or a worker crashes mid-write. **Meta Muse Spark 1.3** reads the customer's
words; deterministic code decides what may be booked, asks before anything the customer did not clearly allow, waits
out outages and recovers from crashes without booking twice. It books through **FieldDesk**, a small field-service
scheduling API built for this project.

> Meta Global AI Developer Hackathon 2026, Indie track. Synthetic customers and data, one laptop. Every number below
> is a measured value from a retained run, failures included.

## Why

A week of measurements came first (the study, below). Muse Spark agents booked 10:00 for a customer who wrote
"6 pm only": the agent said "Done!", the API said 200 OK, and only an independent database check noticed. Under
back-pressure the same agents retried before Retry-After every time, because their tools gave them no way to wait;
adding a `wait` tool took early retries from 60 of 60 to 0 of 60. The failures sat in the interface between the model
and the system. Resolve puts its guarantees there.

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
- **Confirm before booking.** The customer sees the exact slot and must say yes before anything is written.
- **Failure handling.** Outages release the request with backoff (honoring Retry-After). A crash after a write is
  reconciled by resending the same key (`replayed`, no second write). A correction or decline takes effect only after
  any write in doubt is settled. Customer emails and calendar updates are queued in the same transaction as the state
  change and retried.
- **Integrations.** Questions and outcomes go to the customer by SMTP; each booking is mirrored into the technician's
  calendar over CalDAV. Locally both are containers (a test inbox, a Radicale server any calendar app can read).

Design and failure table: [architecture](docs/architecture.md); decisions: [docs/decisions](docs/decisions/).

**When not to use AI here:** a customer who taps a link should get a slot picker. Resolve is for requests that arrive
as text (SMS replies, email, call notes) with conditions a picker cannot record.

## Use of Meta AI

| Model | Where | What it does |
|---|---|---|
| Muse Spark 1.3 (Meta Model API, Responses endpoint, tool calling) | `resolve/interpret.py`, `resolve/models.py`, `harness/provider.py` | Turns a customer message into a typed intent (one tool call against a JSON schema, at most one repair turn) and a later message into explicit patch operations. Capped per process by calls and dollars. |
| Llama 3.1 8B (open weights, local via Ollama) | `resolve/eval/run.py` | Compared in the same pipeline: 10/24, mostly refusals rather than wrong bookings. |

Development used a free local model (qwen3-14b via Ollama); every Muse number below is from a paid, capped run.

## Results

24 held-out conversations, written and frozen before any model saw them, each judged in SQL against its expected
outcome ([protocol](docs/resolve_protocol.md), [results](docs/resolve_results.md)):

| Arm | Correct | Unauthorized bookings | Spend |
|---|---|---|---|
| Resolve + confirm before booking (Muse) | 23/24 (19 bookings + 4 correct refusals) | 0 | $0.25 |
| Resolve without confirmation (Muse) | 23/24 | 1 | $0.23 |
| Same model in one shot (no history, no questions) | 15/24 | 2 | $0.13 |
| Hand-written rule parser, frozen before the held-out set | 20/24 | 2 | $0 |
| Form with correct entries, same execution path | 24/24 | 0 | $0 |
| Llama 3.1 8B in the same pipeline | 10/24 | 2 | $0 |

The pass mark set beforehand was at least 22/24 and zero unauthorized bookings. Without confirmation the one miss,
"same time of day as my current booking", was read as "morning": real words with the wrong meaning, which validation
cannot catch. Confirmation is the guard. Zero unauthorized assumes a customer who reads each proposal.

**Recovery checks:** 20 of 20 targeted checks pass against the live stack (`make resolve-faults`): crashes at three
points, a real lost acknowledgment, connection resets, silent links, a superseded write voided and never resent, a
correction arriving while a write is in doubt, a paused worker, backoff, email and calendar retries.

## Run it

**Prerequisites:** Linux, or Windows 11 with WSL2 (keep the repository in the Linux filesystem); Docker with Compose;
[uv](https://docs.astral.sh/uv/); at least 8 logical CPUs (compose pins containers to CPUs 2–6). A Meta Model API key
only for the Muse paths.

```bash
git clone https://github.com/sasikiran1/rebound && cd rebound
cp .env.example .env          # add MODEL_API_KEY for the Muse paths
uv sync
make up                       # PostgreSQL 16, Toxiproxy, FieldDesk
make seed                     # seeded synthetic template database
make preflight                # trial database from the template; FieldDesk restarted and checked
make resolve-up               # calendar server and test inbox
make check                    # lint, unit tests, no sleep calls in the backend
```

**No model key needed:**

```bash
make resolve-faults           # 20 recovery checks against the live stack
make resolve-form             # the 24 held-out cases through the form arm, judged in SQL
docker compose -f docker/compose.yml --profile resolve up -d   # deployed worker and service
uv run python scripts/resolve_service_e2e.py --form            # intake -> worker -> booking -> calendar -> SQL
```

**Live demo (Muse):**

```bash
make resolve-live MODEL=muse-spark-1.3    # capped at 20 calls / $0.45; open http://127.0.0.1:8092/
```

Click **Rebound**, then **▶ Play scenario**: a messy request, the booking system going down, the customer's first
choice taken during the outage, a question, a correction, a confirmation, and a worker killed right after its write,
ending in one correct change and the event in the technician's calendar (about 2 Muse calls, $0.02). **One-shot
baseline** runs the same model without history, questions or confirmation on the same world. The launcher stops the
`resolve-worker` container while the screen runs; start it again with
`docker compose -f docker/compose.yml --profile resolve start resolve-worker`. `make down` stops everything.

## The study (background)

From Oct 27 to Nov 2, real Muse Spark agents rescheduled appointments through FieldDesk while Toxiproxy slowed its
database link, judged in SQL. Findings: with no way to wait, agents retried before Retry-After every time (69/69,
74/74, 68/68; 139/139 under a 20 s fault), and never once given a `wait` tool; an admission gate cut recovery from
20–22 s to 10 s; and the starting thesis, that agents delay recovery, was not supported (agents were 0.4–1.9% of
in-flight time). The study's code, frozen protocols, freeze records and every result are at the
[`study-final`](https://github.com/sasikiran1/rebound/tree/study-final) tag; raw trial bundles are in the
[`evidence-v1`](https://github.com/sasikiran1/rebound/releases/tag/evidence-v1) release.

## Limitations

- Synthetic data, scripted customers and hand-written cases; 24 held-out conversations is a prototype check, not a
  reliability estimate, and the author wrote the cases, which favors the rule parser.
- The recovery checks are targeted checks of the mechanism under the injected conditions, one run each.
- No authentication: every service binds to 127.0.0.1. A local test inbox and CalDAV server stand in for real
  providers; the calendar is a one-way mirror.

## Repository

| Path | Contents |
|---|---|
| `resolve/` | Intent contract, interpretation, evaluator, conversation service, store, worker, notifications, calendar sync, HTTP service |
| `resolve/eval/` | Case seeding, evaluation arms, SQL oracle, recovery checks, scripted demo, live console and screen |
| `data/resolve/` | 6 dev cases, 24 frozen held-out cases, 4 fault fixtures |
| `backend/` | FieldDesk, the booking API (admission gate, key fencing) |
| `harness/` | Settings, database setup, Toxiproxy control, model clients |
| `data/` | Database schema and the seeded generator |
| `prompts/` | Resolve's interpretation and revision prompts (v1 kept for the development record) |
| `docker/` | Compose stack; Resolve containers under the `resolve` profile |
| `ui/` | Resolve screen and live demo screen |
| `tests/`, `scripts/` | Unit tests; [scripts](scripts/README.md) |

Built during the hackathon, Oct 27 – Nov 4, 2026. MIT license.
