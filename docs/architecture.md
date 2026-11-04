# Architecture

Rebound has two parts that share one backend. The **study** (`harness/`, `oracle/`, `analysis/`) measures real
tool-using agents against FieldDesk under injected faults. **Resolve** (`resolve/`) is a scheduling-recovery assistant
built on the lessons: the model interprets, deterministic code decides and executes, SQL judges. This page covers
Resolve; the study's design is in [spec.md](spec.md) and [protocol.md](protocol.md).

## Components

```text
                       customer text
                            |
            +---------------v----------------+        Meta Model API (Muse Spark)
            |  resolve-service (FastAPI)     |------> or local Ollama (development)
            |  resolve/app.py                 |        model proposes an intent / a patch
            |   intake: service.py           |
            |   interpret.py -> intent.py    |  validate against the customer's words
            |   screen + read APIs           |  and SQL-backed facts; never trusted as-is
            +---------------+----------------+
                            | approve version n (customer turn)
                            v
            +--------------------------------+
            |  resolve store (PostgreSQL,    |  requests, versions, turns, events
            |  schema `resolve`, direct port)|  state machine + lease + in-flight attempt
            +---------------+----------------+
                            | claim (FOR UPDATE SKIP LOCKED, lease)
                            v
            +--------------------------------+        +-----------------------------+
            |  resolve-worker                |  HTTP  | FieldDesk API               |
            |  worker.py: reconcile, search  |------->| /customers/{id}/context     |
            |  all pages, evaluate.py decide,|        | /availability/search (paged)|
            |  record attempt, POST          |        | POST .../reschedule         |
            +--------------------------------+        |   Idempotency-Key           |
                                                      +--------------+--------------+
                                                                     | SQL via Toxiproxy (fault point)
                                                                     v
                                                      +-----------------------------+
            resolve/eval/oracle.py  -- read-only -->  | PostgreSQL 16: slots,       |
            (judge, not part of the product)          | appointments, changes       |
                                                      +-----------------------------+
```

| Module | Responsibility | Trusts the model? |
|---|---|---|
| `resolve/interpret.py` | Builds facts, calls the model with one tool, one repair turn with the validator's problems | produces proposals only |
| `resolve/intent.py` | Typed intent schema; `validate` (quotes, dates, windows, technicians, ambiguity); `apply_patch` (explicit operations; kind-preserving replace); `explain` | no |
| `resolve/evaluate.py` | Pure function: intent + every open slot -> book / permission / clarify / no_options | no model involved |
| `resolve/service.py` | Conversation: open, customer turns, versioned approval, deterministic "yes" in confirm mode | gates every model output through validate |
| `resolve/store.py` | Durable state machine, versions, turns, events, leases, in-flight attempt | n/a |
| `resolve/worker.py` | Claims, reconciles, searches, decides, records the attempt, writes; crash points for tests | no model involved |
| `resolve/notify.py` | Delivers queued customer messages over SMTP, with retry and backoff | no model involved |
| `resolve/calendar.py` | Mirrors each booking into the technicians' CalDAV calendars, with retry and backoff | no model involved |
| `resolve/api.py` | FieldDesk client: one attempt per call, 3 s deadline, paging, call log | n/a |
| `resolve/models.py` | Meta API or Ollama behind one `Model` with call and spend caps | n/a |
| `resolve/app.py` | Screen, read APIs, health, metrics, intake | n/a |
| `resolve/eval/` | Cases and seeding, runner arms, SQL oracle, recovery checks, scripted demo | judges the product |

## State ownership

- **FieldDesk's tables** are the source of truth for appointments. Resolve changes them only through the API.
- **The `resolve` schema** owns the conversation and the approved intent. It is reached on the direct port, so a
  FieldDesk database-link outage (the Toxiproxy fault) does not take it down; it is in the same PostgreSQL instance,
  so an instance outage would (see [limitations](resolve_results.md#limitations)).
- **The oracle** reads both through a read-only role and owns nothing.

## Request lifecycle

```text
          customer message            worker: book (confirm off, or slot approved)
 (new) ---------------------> ready ------------------------------------------> completed
                               ^  |  worker: permission / clarify / no_options / confirm
                               |  v
              customer turn    needs_input --- customer declines / 3 questions ---> unresolved
              approves v(n+1)
          claimed: between "ready" and the outcome; while claimed no new version can be approved
```

## Failure handling

| Failure | What happens | Tested by |
|---|---|---|
| FieldDesk slow or down (search or write times out, 5xx) | Request released back to `ready` with a backoff (the server's Retry-After in full, or 1 s doubling to 30 s with jitter); a write in doubt keeps its attempt record | demo outage; `lost_ack`; `backoff` |
| Worker dies after claiming | Lease expires; another worker claims and runs from the start | `crash_after_claim` |
| Worker dies after recording the attempt, before sending | Next claim resends the recorded slot with the same key; it commits once | `crash_after_record` |
| Worker dies after the write committed | Next claim resends with the same key; FieldDesk returns the original change (`replayed`) | `crash_after_send`, demo |
| Reply lost, commit lands later | Same as above; a resend that races the commit gets `same_slot` and asks again | `lost_ack` |
| Slot taken between search and write | 409: search again (at most 3 times) | runner, worker |
| Approved slot taken before execution | Not authorized any more: a new question, never a different slot | `renewed_permission` |
| More than 50 open slots | Paged until `total_count` | `truncation` |
| Model output invalid (dates, windows, technicians, quotes, schema) | One repair turn; still invalid: unresolved, no booking | `invalid_intent` |
| Correction rewrites an unrelated rule | `replace_rule` must keep the kind; otherwise rejected and repaired | `correction_preserves` |
| Model misreads but quotes real words | Not caught by the validator; confirm mode shows the exact slot before any write | held-out H15; `confirm_mode`; held-out with confirm: 0 unauthorized |
| Malformed model output (wrong types) | Schema check before anything else; a repair turn; never a crash | Llama 3.1 8B run; unit test |
| Database link reset in the middle of the write | FieldDesk answers 500; the attempt stays uncertain; reconcile commits exactly once | `fault_shapes` |
| Database link silent during the search | Timeout; request released; booked once the link is back | `fault_shapes` |
| Mail server refuses or is down | The message stays queued with its error; retried with backoff (5 s, 10 s, ...); failed after 6 attempts | `notifications` |
| Calendar server down when the booking completes | Booking unaffected; the calendar sync stays queued and catches up; stale copies in other technicians' calendars removed | `calendar` |
| Customer corrects or declines while a write is in doubt | The conversation voids the old key before the turn takes effect (a late copy is then refused with `key_voided`); if FieldDesk is unreachable the correction or close waits as pending, and the worker settles the write first, then applies it and tells the customer if the write had gone through. The old version's attempt is never resent | `superseded_attempt`, `superseded_committed`, `correction_settles_first`, `correction_waits_for_settlement`, `decline_settles` |
| Worker pauses past its lease (GC, CPU starvation) | Another worker claims a new lease generation; the paused worker's renew before the POST (or resend) fails, so it does not send, and its store writes no longer match the generation. A POST already in flight is not fenced by the lease; the key and voiding cover it | `stale_worker` |
| Crash between a state change and the customer message | Not possible: state, assistant turn and notification are one transaction | `atomic_question` |

Guarantee, as tested: with FieldDesk's idempotency keys and key fencing, every injected crash point ends with at most
one applied change; a superseded write is never resent; and a correction or decline takes effect only after the
earlier write is settled, so an old write never commits after the correction took effect. This
is duplicate suppression and recovery under the tested conditions, not a general exactly-once claim. Not covered:
multi-tenant isolation, a shared spending limit across processes, failure of the PostgreSQL instance itself, and
recovery of notifications or calendar syncs that exhausted their retries (they stay `failed` for an operator).

## Deployment

`docker compose -f docker/compose.yml --profile resolve up -d` adds four containers to the existing stack, from one
image (`docker/resolve.Dockerfile`, non-root user):

- `resolve-worker`: `python -m resolve.worker` in service mode; claims any ready request, delivers due customer
  messages (`resolve/notify.py`) and calendar syncs (`resolve/calendar.py`); on SIGTERM finishes the request in hand
  and exits 0.
- `resolve-calendar`: a CalDAV server (Radicale, `resolve/eval/calserver.py`) on `127.0.0.1:5232` with one login,
  storing calendars in `var/calendar`; any calendar client can subscribe to `/fielddesk/tech-<id>/`. A hosted CalDAV
  provider replaces it by `RESOLVE_CALDAV_URL / USER / PASSWORD`.
- `resolve-service`: `uvicorn resolve.app:app`, published on `127.0.0.1:8091`; `/healthz` checks the store and
  FieldDesk.
- `resolve-mail`: a local SMTP test inbox (`resolve/eval/mailsink.py`) that stores every message in
  `var/mail/inbox.jsonl` and relays nothing. A real provider replaces it by configuration alone.

Configuration (environment): `REBOUND_PG_HOST/PORT`, `FIELDDESK_DB`, `REBOUND_FIELDDESK_URL`, `MODEL_API_KEY`,
`RESOLVE_MODEL` (default `muse-spark-1.3`), `RESOLVE_MAX_CALLS` / `RESOLVE_MAX_COST` (per-process caps, default 50 /
$1.00), `RESOLVE_CONFIRM` (default `true`), `RESOLVE_TODAY` (fixture clock by default), `RESOLVE_SMTP_HOST/PORT`,
`RESOLVE_SMTP_USER/PASSWORD` (STARTTLS when set), `RESOLVE_MAIL_FROM`, `RESOLVE_CALDAV_URL/USER/PASSWORD`,
`RESOLVE_CALDAV_EVENT_MIN`. Verified end to end over HTTP with `scripts/resolve_service_e2e.py`: intake, worker
container, confirmation email, yes, booking, "moved" email, SQL verdict; with `--form` (no model call), booking and
the event in the technician's calendar, read back over CalDAV.

## Why a live stack, not mocks

A mock answers what its author scripted. The checks here run against the real FieldDesk, PostgreSQL and Toxiproxy
because the defects worth finding only exist there:

- a resend racing an in-flight commit gets `same_slot` while it waits on the row lock (would have caused a double
  booking; found by the lost-acknowledgment check);
- a connection reset mid-transaction aborts the write, so the reconcile must commit fresh, not expect a replay;
- the provider's stored-response race and `applied_at` preceding COMMIT (found in the study);
- in the study, realistic load showed agents were under 2% of in-flight time, which disproved the headline
  thesis; a mock built around that thesis would have confirmed it.

The narrow behavioral claims (agents retry early) need no infrastructure, and the refusal probe measures them with a
synthetic refusal instead. Each claim names the setup it needs.

## Security

- The model never reaches SQL or the booking endpoint: its output is JSON validated against a schema and the facts,
  and only the worker writes, through the API, for the request's own customer and appointment.
- `MODEL_API_KEY` is read from the environment only; it is never logged, and `.env` is gitignored (history checked).
- The intake API has **no authentication**; it is published on localhost only. A real deployment needs customer
  authentication and per-customer authorization on every request id.
- The store uses the PostgreSQL superuser for its own schema (prototype shortcut). A real deployment needs a role
  limited to the `resolve` schema.
- Customer messages are stored in the database and sent to the model provider. If the account uses Meta's
  contributor tier, Meta may train on prompts (see `harness/provider.py`). Use synthetic data only, as here.

## Decisions

Short records in [decisions/](decisions/):
[0001 durable queue in PostgreSQL](decisions/0001-queue-in-postgres.md),
[0002 idempotency key per request and version](decisions/0002-idempotency-key.md),
[0003 the model never sees availability](decisions/0003-model-without-availability.md),
[0004 confirm before booking](decisions/0004-confirm-before-booking.md),
[0005 retries in middleware, not a model tool](decisions/0005-retries-in-middleware.md),
[0006 notifications from a transactional queue](decisions/0006-notifications.md),
[0007 technicians' calendars mirrored over CalDAV](decisions/0007-calendar-sync.md),
[0008 key fencing, lease generations, backoff and atomic customer messages](decisions/0008-fencing-and-atomic-messages.md).
