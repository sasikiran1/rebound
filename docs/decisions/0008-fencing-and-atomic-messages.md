# 0008 Key fencing, lease generations, backoff and atomic customer messages

**Status:** accepted, Nov 3, 2026. Prompted by an outside review of the code, which found four defects; each was
confirmed before it was fixed.

**Context.** The idempotency key made a resend safe, but it did not answer a different question: may this write still
be sent at all? After a timeout the request went back to `ready` with its attempt kept. If the customer then
corrected the request, the next worker resent the old version's slot and key before looking at the new version, so a
write the customer had just withdrawn could still be booked. Three related gaps: a state change and its customer
message were separate writes; a released request was claimed again at once, ignoring Retry-After; leases had no
renewal or generation, and compose replicas shared one owner name.

**Decision.**

- **Key fencing in FieldDesk.** Where the table `idempotency_keys` exists, a reschedule claims its key there in the
  same transaction (`applied`). `POST /idempotency/{key}/void` claims it as `voided` when no change exists, or returns
  the change if it committed. The primary key serializes the two, so a write still in flight when its key is voided
  fails with `409 key_voided` and writes nothing.
- **The worker never resends a superseded attempt.** If the recorded attempt's key is not the current version's key,
  the worker voids it, records what happened (`worker.superseded_attempt`: applied or voided), and runs the current
  version. If the old write had committed, the appointment then moves to what the new version allows: two changes,
  each authorized when it was made.
- **Lease generations.** Every claim increments `lease_gen`; state changes and attempt records count only for the
  owner and generation that claimed. The lease is renewed right before the booking POST, and a worker that cannot
  renew does not send. Worker names are host plus pid.
- **Backoff.** A released request carries `not_before`: the server's Retry-After, else 1 s doubling to 30 s with
  jitter. The service loop honors it; a run aimed at one request (a check, the demo, the live console) paces itself.
- **Atomic messages.** `Store.set_state(..., turn=(text, kind))` writes the state, the assistant turn and its
  notification in one transaction.

**Why only where the table exists.** The study's template database is checksummed and its trials were recorded
without fencing; FieldDesk enables fencing only when the table is present, and the Resolve store creates it in the
trial database. Study results and their verification are unchanged.

**Consequences.** Checked on the live stack (`runs/resolve/faults/faults-r8`, 17 of 17): `superseded_attempt`,
`superseded_committed`, `stale_worker`, `backoff`, `atomic_question`, plus the earlier twelve unchanged. Still not
covered: overlapping workers beyond the paused-worker case, multi-tenant isolation, a shared spending limit across
processes, and recovery of messages or calendar syncs that exhausted their retries.
