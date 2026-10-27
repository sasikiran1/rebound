# Results log

Numbers only. Each day appends what was measured.

## Day 1 · Tue Oct 27, 2026

### Environment (step 0)

| Item | Value |
|---|---|
| Host | Windows 11 Home 10.0.26300, Intel Core Ultra 9 275HX (8 P + 16 E cores, no SMT) |
| WSL | Ubuntu **26.04 LTS**, kernel 6.6.87.2-microsoft-standard-WSL2 |
| `nproc` (WSL) | 24 |
| Memory visible to WSL | 15 GiB total, 14 GiB available |
| Docker | Engine 29.2.1 (Docker Desktop, WSL integration), Compose v5.1.0, `hello-world` OK |
| Python / uv (WSL) | 3.12.16 (installed by uv) / uv 0.13.6 |
| Repo location | `~/rebound` (Linux filesystem) |

Proposed CPU split (vCPU indices inside the WSL VM):

| Component | cpuset |
|---|---|
| postgres | 2-3 |
| fielddesk | 4-5 |
| toxiproxy | 6 |
| runner (`taskset`) | 8-15 |
| left free (kernel, dockerd, desktop) | 0-1, 7, 16-23 |

Note: the CPU is hybrid (P + E cores). Hyper-V schedules WSL vCPUs onto host cores, so pinning inside
the VM does not guarantee a container stays on P-cores. This is a known variance source; baseline p95
stability across healthy runs (Day 4 check) is what decides whether it matters.

Project renamed from "Aftershock" to **Rebound** on Day 1 (identity headers `X-RB-*`, cache key `rebound-agent-v1`).

Docker Desktop pulls through its internal proxy (`http.docker.internal:3128`) failed intermittently with
blob-download EOFs; 3-6 retries succeeded. Direct downloads from WSL were fine. Images are pinned by digest
and cached locally, so this does not affect trials.

### Images (pinned by digest)

| Image | Digest |
|---|---|
| postgres:16 (16.15) | `sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94` |
| ghcr.io/shopify/toxiproxy:2.12.0 | `sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e` |
| python:3.12-slim (fielddesk base) | `sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f` |

### Data (1/10 scale, seed 1729)

| Table | Rows |
|---|---|
| regions / region_distances | 20 / 400 |
| skills / service_types | 6 / 10 |
| technicians / technician_skills / technician_regions | 40 / 97 / 125 |
| customers | 4,000 |
| slots | 13,440 (21 days × 16 × 40) |
| appointments | 10,166 (~70% of slots scheduled, ~5% cancelled records) |

Template checksum `sha256:f2fc24ee7d413b77f65e3cbcedcbc02f17119f897c4568fe2415251ea1e9a298`; identical on two
generator runs; clone verification passes. Build time 0.6 s.

Task set A (dev): 40 tasks = 24 one-day, 12 three-day, 4 infeasible. An independent SQL check confirmed every
task's free-candidate count and appointment identity (0 mismatches). Tasks are non-interfering: disjoint free
candidates, and no task's original slot qualifies for another task's window.

### Per-endpoint DB time (healthy, sequential, n = 100 each, 1/10 scale)

| Route | DB ms p50 | DB ms p95 | Handler ms p50 | Queries |
|---|---|---|---|---|
| GET /customers/{id}/appointments | 1.17 | 1.59 | 1.58 | 2 |
| GET /appointments/{id} | 0.83 | 1.08 | 1.24 | 1 |
| GET /availability (one day) | 1.16 | 1.58 | 1.73 | 2 |
| POST /appointments/{id}/reschedule | ~2.6-3.0 (smoke test, n = 3) | | | 4-6 |

DB time includes the round trip through toxiproxy and asyncpg's connection reset when a connection goes back
to the pool (one more round trip per request). At this scale all reads are dominated by round trips, not query
work. The ≥ 10× broad-search target is a Day 2 check, after `search_availability` and full-scale data exist.

### Latency pulse (`make pulse`: 400 ± 50 ms downstream on `pg`, 10 s; 1 Hz; medians)

| Phase | Canary ms | FieldDesk client ms | FieldDesk DB ms |
|---|---|---|---|
| before (10 s) | 1.2 | 5.5 | 2.6 |
| during (10 s) | 392.7 | 1180.7 | 1177.3 |
| after (10 s) | 0.7 | 4.1 | 1.7 |

Canary rise 391.5 ms = 98% of the configured 400 ms; it passes the ≥ 70% fault-verification guard. The
`/availability` request makes 3 round trips (2 queries + pool reset), so its DB time rose by ~3 × 400 ms. Before
the pool reset was counted, DB time covered only 809 of the 1222 ms client latency during the fault; now it
covers 1177 of 1181 ms. Recovery after the toxic was removed was immediate at 1 Hz resolution with no load.

### Offline pipeline check (no model)

A scripted stand-in for the model drove the real adapter → FieldDesk → oracle path on a fresh clone:
- one-day task A0001 → rescheduled → oracle `correct`; completion time taken from `appointment_changes.applied_at`.
- infeasible task A0003 → deferred → oracle `correct`.
- 6 `http.attempt` client events and 6 backend rows, all 6 joined by `attempt_id`.

Tests: 49 passing (oracle verdict classes, adapter validation and scope, provider retry table, agent loop limits).

### Live agent goal: NOT RUN

`MODEL_API_KEY` is not set in Windows or WSL, so the verification call and the live goal are pending. No model
spend so far ($0.00).

### Design interpretations (please confirm)

1. **At most one write per goal** is read as at most one *applied* write. After a 409, timeout or 503 the model
   may call `reschedule_appointment` again with the same derived idempotency key. If the earlier attempt
   actually committed, the backend returns that original change, never a second one.
2. **Ownership check:** the adapter adds the goal's `customer_id` to the reschedule body; the model never sets it.
3. **`get_appointment` scope:** allowed for the goal's appointment and for any appointment the customer listing
   returned.
4. **The backend validates technician skill and coverage** on reschedule (422 `unqualified_slot`). So `incorrect`
   verdicts can only come from date-window violations, extra writes, or writes on infeasible goals.
