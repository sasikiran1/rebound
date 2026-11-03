"""Durable intent outbox (Nov 2 prototype, kept as an archived experiment).

Superseded by Resolve (`resolve/`), which adds versioned customer approval, questions when availability changes,
reconcile-before-search, leases and paged search. This prototype has none of those: when no stored slot fits, it
records `no_slot` and nobody tells the customer. It was never run in a paid trial. Its tests stay so it keeps working.

When the API is overloaded, an agent can call `store_intent`: it writes what the customer asked for, as structured
requirements it parsed from the customer's words, to `outbox.intent_outbox`, and its goal ends as `handed_off`.
After the fault clears, `drain` (deterministic code, no model) completes each stored intent through the normal API:
it searches the stored date range and start-time window, keeps only slots that meet every stored requirement, and
books the first one with the goal's own idempotency key, so a write the agent already landed is replayed, never
doubled. The SQL oracle then judges the booking against the task's hidden constraint, so a wrong parse is caught.

The table lives in its own schema in the trial database and is written over the direct port, not through
Toxiproxy: it stands for the agent runtime's durable store, which the fault does not slow. The trial database is
re-cloned from the template for every trial, and the template fingerprint does not cover this schema.
"""

from __future__ import annotations

import json
import random

import asyncpg

from harness.adapter import GoalScope, GoalState, RetryPolicy, ToolAdapter, requirement_problem

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS outbox;
CREATE TABLE IF NOT EXISTS outbox.intent_outbox (
    id             bigserial PRIMARY KEY,
    trial_id       text        NOT NULL,
    goal_id        text        NOT NULL,
    goal_n         integer     NOT NULL,
    customer_id    integer     NOT NULL,
    appointment_id integer     NOT NULL,
    regions        text[]      NOT NULL,
    intent         jsonb       NOT NULL,
    status         text        NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'claimed', 'done', 'no_slot', 'failed')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    claimed_at     timestamptz,
    finished_at    timestamptz,
    change_id      bigint,
    detail         text,
    UNIQUE (trial_id, goal_id)
);
"""

# The worker searches with start-time filters (tool set v2); it needs those schemas, not the agent's.
WORKER_TOOLS = "prompts/tools_v2_time.json"
# Honors Retry-After; full-jitter backoff otherwise (seeded, so a re-run waits the same). For transient refusals
# right after recovery.
WORKER_RETRY = RetryPolicy(max_retries=4, rng=random.Random(20261102))


class Outbox:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def put(self, scope: GoalScope, intent: dict) -> int:
        """Store (or, while still pending, replace) a goal's intent. Customer, appointment and regions come from the
        goal's scope, never from the model, so an intent can only ever act on the task's own appointment."""
        return await self.pool.fetchval(
            """
            INSERT INTO outbox.intent_outbox (trial_id, goal_id, goal_n, customer_id, appointment_id, regions, intent)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (trial_id, goal_id) DO UPDATE SET intent = EXCLUDED.intent
                WHERE outbox.intent_outbox.status = 'pending'
            RETURNING id
            """,
            scope.trial_id, scope.goal_id, scope.goal_n, scope.customer_id, scope.appointment_id,
            list(scope.allowed_regions), json.dumps(intent),
        )

    async def claim(self, trial_id: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """
            UPDATE outbox.intent_outbox SET status = 'claimed', claimed_at = now()
            WHERE id = (SELECT id FROM outbox.intent_outbox WHERE trial_id = $1 AND status = 'pending'
                        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *
            """,
            trial_id,
        )

    async def finish(self, row_id: int, status: str, change_id: int | None = None, detail: str | None = None):
        await self.pool.execute(
            "UPDATE outbox.intent_outbox SET status = $2, change_id = $3, detail = $4, finished_at = now() "
            "WHERE id = $1",
            row_id, status, change_id, detail,
        )


async def complete(row: asyncpg.Record, adapter: ToolAdapter) -> tuple[str, int | None, str]:
    """Complete one stored intent. Returns (status, change_id, detail); status is done | no_slot | failed."""
    intent = json.loads(row["intent"]) if isinstance(row["intent"], str) else dict(row["intent"])
    scope = GoalScope(row["trial_id"], row["goal_n"], row["customer_id"], row["appointment_id"], list(row["regions"]),
                      actor_kind="worker")
    state = GoalState()
    appt = await adapter.call(scope, state, "get_appointment", {"appointment_id": row["appointment_id"]},
                              retry=WORKER_RETRY)
    if "error" in appt:
        return "failed", None, f"get_appointment: {appt['error']['type']}"
    search = {"service": appt["service"], "date_from": intent["date_from"], "date_to": intent["date_to"],
              "regions": list(row["regions"])}
    search.update({k: intent[k] for k in ("earliest_start", "latest_start") if intent.get(k)})
    if "earliest_start" in search:
        search["start_after"] = search.pop("earliest_start")
    if "latest_start" in search:
        search["start_before"] = search.pop("latest_start")
    found = await adapter.call(scope, state, "search_availability", search, retry=WORKER_RETRY)
    if "error" in found:
        return "failed", None, f"search_availability: {found['error']['type']}"
    for slot in found.get("slots", []):
        if requirement_problem(intent, slot) is not None:
            continue
        booked = await adapter.call(scope, state, "reschedule_appointment",
                                    {"appointment_id": row["appointment_id"], "slot_id": slot["slot_id"]},
                                    retry=WORKER_RETRY)
        if "error" not in booked:
            return "done", booked.get("change_id"), f"slot {slot['slot_id']} at {slot['starts_at']}"
        if booked["error"]["type"] != "http_409":  # taken by someone else: try the next slot
            return "failed", None, f"reschedule: {booked['error']['type']}"
    return "no_slot", None, f"{len(found.get('slots', []))} slots searched, none met the stored requirements"


async def drain(outbox: Outbox, adapter: ToolAdapter, trial_id: str, events=None) -> list[dict]:
    """Complete every pending intent of a trial, one at a time. Call only after the fault has been removed."""
    results = []
    while (row := await outbox.claim(trial_id)) is not None:
        status, change_id, detail = await complete(row, adapter)
        await outbox.finish(row["id"], status, change_id, detail)
        result = {"intent_id": row["id"], "goal_id": row["goal_id"], "status": status, "change_id": change_id,
                  "detail": detail}
        if events is not None:
            events.emit("outbox.completed", goal_id=row["goal_id"], actor_kind="worker", **{
                k: v for k, v in result.items() if k != "goal_id"})
        results.append(result)
    return results
