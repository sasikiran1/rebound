# 0002 Idempotency key per request and approved version

**Status:** accepted, Nov 3, 2026.

**Context.** A write can commit while its reply is lost (worker crash, timeout). Retrying blindly could book twice.
A hash of the intent was proposed as the key; two different requests can have identical content, and one request's
intent changes with each approval.

**Decision.** Key = `resolve:<request_id>:v<version>`. The worker records `(slot, key, sending)` before the POST and
reconciles first on the next claim: resend the same slot with the same key. FieldDesk returns the original change for
a committed key (`replayed`) and writes nothing. A 409 means the key never committed. `same_slot` means the first
attempt committed while the resend waited on its row lock: ask again, never treat it as "not committed" (found by the
lost-acknowledgment check).

**Consequences.** Relies on FieldDesk's key table and on freezing the version while a request is claimed (new
approvals are refused). Tested at three crash points and with a real lost acknowledgment; not a general exactly-once
guarantee.
