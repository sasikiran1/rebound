# Goal preservation: bounded research prototype

This is an experimental endpoint, not an addition to historical FieldDesk routes. It implements the proposed
goal-preservation interaction before collecting any live-agent evidence. It does not establish originality.

## Contract and acceptance

Each trusted fixture registers an immutable goal ID, an explicit acceptable-slot set and an expiry. Every slot
has unit capacity. A proposed booking is allowed only when committing it preserves the maximum number of
achievable currently registered, unexpired goals, counting that proposed completion. A rejected proposal returns
alternative slots satisfying the requesting goal and preserving that maximum. When all goals are jointly
feasible, this preserves their collective feasibility. When they are not, it preserves a count, not each identity.

SQLite serializes registration and commit writers with BEGIN IMMEDIATE. Commit checks the revision and recomputes
feasibility in that transaction. A recommendation is advisory, not an authorization token. Registration changes
invalidate old revisions; expiry is checked again even without a revision change. Repeated successful commits
for the same goal and slot replay without another write. A different slot for a completed goal conflicts.

Stop condition for this first milestone: demonstrate a blocked avoidable loss, successful alternatives, SQL-graded
outcomes, stale-recommendation rejection, persistence and concurrent commit safety. No model inference is needed
to verify those mechanics. Live-agent testing is a separate future cohort, not substituted by the demo.

## Run locally

From the repository root with development dependencies installed:

```sh
PYTHONPATH=backend:. .venv/bin/python scripts/goal_preservation_demo.py --output runs/goal-preservation/demo-001
.venv/bin/pytest -q tests/test_goal_preservation.py
```

The output directory must be new. It contains three SQLite databases, complete constructed-policy traces and
an independent SQL audit. The demo sends HTTP requests through an in-process ASGI transport; it does not claim
network or production testing. Baselines are scripted first-choice booking and a competent constrained-first
central scheduler. They are not live agents. The central scheduler is expected to solve this simple fixture too.

To explore the API over loopback after seeding its database via Store.seed_slots:

```sh
PYTHONPATH=backend:. .venv/bin/uvicorn fielddesk.goal_preservation:create_app --factory --host 127.0.0.1 --port 8091
```

POST /goals registers a goal; GET /goals/{id}/proposal?slot_id=15 evaluates; POST /goals/{id}/commit accepts
`{"slot_id": 9, "version": 3}` using the version actually returned by the proposal. Default database:
var/goal-preservation.sqlite. No authentication is implemented: this is a trusted, loopback-only research service.

## Independent verification and limits

The oracle reads SQLite directly and exhaustively enumerates allocations, without importing the production
matching algorithm or trusting API receipts. It checks known slots, registered constraints, uniqueness,
completed goals and residual achievable completions. It supports at most eight goals. Its comparison is a static
audit at the supplied time; it does not reconstruct historical expiry or authorization. The endpoint supports
at most 64 goals and 64 acceptable slots per goal. Request bodies are trusted fixture inputs, not proof that
natural-language preferences were correctly interpreted.

Receipts omit other goal IDs and their raw constraints. This is not a formal privacy guarantee. No minimality
certificate, cryptographic proof, fairness policy, cancellation, multi-resource duration, real customer
authorization, external-writer safety or FieldDesk/Postgres integration is claimed. Every database writer in
the protected path must participate in the protocol. Unknown future goals cannot be protected. This is not
a guarantee that an agent will finish, or that a booking will survive external resource changes.

## Next measurement, before any claim of agent benefit

Freeze tasks, two local model artifacts, prompts, seeds and budgets before inference. Compare ordinary
uniqueness, a competent centralized scheduler and the proposed endpoint with matched goal sets and arrival
orders. Keep every session and separately grade completion, unnecessary refusal, constraints, calls, latency
and receipt use. Include already-infeasible and no-contention controls. A deterministic toy improvement cannot
establish model benefit. If ordinary scheduling supplies the same capabilities more simply, that weakens the
case for this protocol. Existing matching and shielding work must be compared before claiming research novelty.
