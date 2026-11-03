# 0001 Durable queue in PostgreSQL

**Status:** accepted, Nov 3, 2026.

**Context.** A request must survive a FieldDesk outage and a worker crash, and be finished later. Options: Celery
with a broker, AWS SQS, or a table in the PostgreSQL instance the stack already runs.

**Decision.** A table (`resolve.requests`) claimed with `FOR UPDATE SKIP LOCKED` and a lease. The same row holds the
state machine, the approved version and the in-flight attempt, so a claim, a state change and the attempt record are
one transaction away from each other.

**Consequences.** No new infrastructure; easy to inspect with SQL; the oracle can read it. One worker per request at a
time is all that is tested. It is the standard job-queue / transactional-outbox pattern, not a new mechanism; a
broker would work the same with the same idempotency key. The store shares the database instance with FieldDesk (not
the faulted link), so an instance outage stops both.
