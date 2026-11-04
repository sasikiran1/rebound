# Case study: making AI agents safe to act on real bookings

*Rebound, Meta Global AI Developer Hackathon 2026 (Indie track). Synthetic data, scripted customers, one machine.*

## The problem

A tool-using agent says "Done!", the API returns 200 OK, and the booking is wrong. In our study Muse Spark booked
10:00 for a customer who wrote "6 pm only"; only an independent check of the database noticed. Separately, when the
API pushed back with 503 and Retry-After, the agents retried early every single time.

## What I built

1. **A measurement harness** (Python, FastAPI, PostgreSQL 16, Toxiproxy, Docker Compose): real Muse Spark agents
   reschedule appointments on a purpose-built API while its database link is slowed; every outcome is judged by an
   independent SQL oracle, every run frozen, checksummed and kept, failures included.
2. **Resolve**, a scheduling-recovery assistant: the model turns a customer's words into typed, versioned constraints
   that must quote those words; deterministic code books only what the customer authorized, asks before any
   trade-off with real open slots, survives worker crashes and lost acknowledgments without double-booking, and
   confirms the exact slot before writing, emails the customer and mirrors each booking into the technician's
   calendar over CalDAV. Deployed as worker, service, mail and calendar containers with an intake API.

## Results (measured)

| | |
|---|---|
| Agents' early retries before Retry-After | 100% (69/69, 74/74, 68/68; 139/139) |
| With a runtime guard | 0 early, 209/209 correct goals |
| Admission gate, recovery after a 10 s fault | 20–22 s to 10 s (the metric's floor) |
| Original thesis, "agents delay recovery" | **not supported**: agents were under 2% of in-flight time |
| Resolve, 24 frozen held-out conversations, Muse | **23/24 correct** (19 bookings + 4 correct refusals), **1 unauthorized booking**, $0.23 |
| Same, customer confirms the exact slot first (simulated customer who reads every proposal) | **23/24, 0 unauthorized** |
| Hand-written rule parser, frozen before held-out | 20/24, 2 unauthorized |
| Same model, one-shot extraction | 15/24, 2 unauthorized |
| Form with perfect entries, same execution path | 24/24 |
| Llama 3.1 8B in the same pipeline | 10/24, 2 unauthorized |
| Targeted recovery checks on the live stack | 17/17 (one run each) |
| Total model spend for Resolve | $0.70 (106 calls) |

## Decisions that mattered

- **The model proposes; code decides; SQL judges.** The model never sees availability and never books, so it cannot
  invent a slot. One corrective iteration, mostly structural checks (kind-preserving corrections, ambiguous-name
  detection, grounded history references) plus a revised prompt, took development cases from 3 unauthorized
  bookings in 6 to 0 on the local model.
- **Reconcile before retrying.** One idempotency key per request and approved version; a lost acknowledgment check
  found a real race (a resend waiting on the row lock gets `same_slot`, which must mean "ask again", not "failed").
- **Know when not to use AI.** A customer who taps a link should get a slot picker. Retries belong in middleware, not
  in a model's `wait` tool. The queue and idempotency are standard engineering, presented as such.

## What did not work, and what I would do next

- The study's headline thesis failed at our traffic mix; reported as a null result.
- Resolve's one unauthorized booking quoted the customer's real words with the wrong meaning; validation cannot catch
  that. Confirming the exact slot first removed it on the held-out set (with a simulated customer who always reads).
- A careful rule parser came within 3 cases of the model on conversations I wrote. The model's edge is robustness
  to phrasing; proving that needs messages written by someone else.
- Llama 3.1 8B: 0/24 through raw tool use in the study, 10/24 inside the validated pipeline.
- Next: customer messages written by other people, a hosted calendar and email provider instead of the local CalDAV
  server and test inbox,
  and real scheduling data on how often requests arrive as free text.

Code, evidence and every number: <https://github.com/sasikiran1/rebound>
([architecture](architecture.md), [Resolve results](resolve_results.md), [study results](results.md)).
