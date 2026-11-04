# Goal preservation — first mechanics check

Recorded November 2, 2026. This is a constructed deterministic demonstration, not live-agent evidence.
No paid API, model inference, model download, injected outage or production workload was used.

## What was implemented

A separate FastAPI research endpoint registers trusted finite slot choices, evaluates proposed bookings,
returns alternatives and commits under a SQLite write lock with revision checks. Existing FieldDesk routes,
Postgres schema, historical results and frozen evaluation specifications were not modified.

The independent checker reads final SQLite state and exhaustively enumerates possible allocations. It does not
use the endpoint's matching routine or accept the receipt as ground truth.

## Demonstration and observations

Alice accepts either slot 9 or slot 15, preferring 15. Bob accepts only 15. Both goals are registered before
any booking. The slot IDs are illustrative labels for morning and afternoon, not actual FieldDesk slot IDs.

| Constructed client policy | Completed | Avoidably lost | Valid and unique writes |
|---|---:|---:|---|
| Ordinary first-choice booking | 1/2 | 1 | Yes |
| Centralized constrained-first scheduling | 2/2 | 0 | Yes |
| Proposed goal-preservation endpoint | 2/2 | 0 | Yes |

The endpoint rejected Alice's slot-15 commit and returned slot 9. A scripted client followed that alternative;
Bob then committed slot 15. This verifies the intended mechanics in the constructed case. It does not show a
model understood the receipt, or that this endpoint outperforms ordinary scheduling. The competent central
scheduler also completed both goals. The evidence therefore supports implementing the interaction, not a
claim of unique allocation capability or demonstrated product advantage.

## Checks actually run

- Focused tests: 8 passed. They include exhaustive matching comparison on all 512 three-goal/three-slot graphs,
  HTTP rejection and completion, replay, reopening the persisted database, new-registration invalidation,
  expiry, constraint validation, racing commits and an already-infeasible cohort.
- Full repository test suite: 151 passed.
- Ruff checks: all four new Python files passed.
- Demo: in-process HTTP through ASGI, with actual persisted SQLite writes. No network transport was exercised.

Raw databases, traces and summary: `runs/goal-preservation/demo-20261102-01/`. This is an ignored local results
directory, not a published release. Reproduce with the command in `docs/goal_preservation.md`, using a new output
directory. Neither commits nor publication were performed.

## Required next evidence

First review the protocol against ordinary scheduling and multi-agent shielding prior work. Then freeze a
separate live-agent cohort with the existing local model runtime, matched arrival schedules, explicit goal
constraints and retained failures. Measure whether agents use the alternatives correctly, any extra calls and
latency, and whether the decentralized interaction adds value over a central scheduler. No live-agent result,
privacy guarantee, real FieldDesk integration, or novelty conclusion is claimed by this mechanics check.
