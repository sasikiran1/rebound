"""Durable Resolve state in PostgreSQL (schema `resolve` in the trial database, direct port, not through Toxiproxy:
it stands for the assistant's own store, which the FieldDesk outage does not affect).

A request has one stable identity (`request_id`) and moves through explicit states:

  needs_input  waiting for the customer (a clarification or a permission question is open)
  ready        an approved intent version waits for the worker
  claimed      the worker holds a lease and is executing the frozen version
  completed    the booking is confirmed (change_id recorded)
With `confirm` set (product mode), the worker never books a slot the customer has not explicitly approved: it asks
"shall I book <slot>?" first. The frozen evaluation runs with it off, as the cases were written.
  unresolved   nothing will be booked (declined, unsupported, or no progress)

Every approved intent is a new row in `resolve.versions`; the request points at the version being executed. The
in-flight booking attempt (slot, idempotency key, sending/acked) is written *before* the POST, so a worker that dies
after the commit but before recording the answer leaves enough behind to reconcile instead of booking again.
"""

from __future__ import annotations

import json

import asyncpg

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS resolve;
CREATE TABLE IF NOT EXISTS resolve.requests (
    request_id      text PRIMARY KEY,
    customer_id     integer NOT NULL,
    appointment_id  integer NOT NULL,
    region          text    NOT NULL,
    service         text    NOT NULL,
    today           date    NOT NULL,
    horizon_days    integer NOT NULL,
    state           text    NOT NULL CHECK (state IN ('needs_input', 'ready', 'claimed', 'completed', 'unresolved')),
    version         integer NOT NULL DEFAULT 0,
    question        jsonb,
    questions_asked integer NOT NULL DEFAULT 0,
    lease_owner     text,
    lease_until     timestamptz,
    attempt_slot    integer,
    attempt_key     text,
    attempt_state   text CHECK (attempt_state IN ('sending', 'acked')),
    change_id       bigint,
    booked_slot     integer,
    detail          text,
    facts           jsonb   NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE resolve.requests ADD COLUMN IF NOT EXISTS confirm boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS resolve.versions (
    request_id  text    NOT NULL REFERENCES resolve.requests (request_id),
    version     integer NOT NULL,
    intent      jsonb   NOT NULL,
    approved_by jsonb   NOT NULL,
    diff        jsonb,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (request_id, version)
);
CREATE TABLE IF NOT EXISTS resolve.turns (
    request_id text    NOT NULL REFERENCES resolve.requests (request_id),
    n          integer NOT NULL,
    role       text    NOT NULL CHECK (role IN ('customer', 'assistant')),
    text       text    NOT NULL,
    kind       text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (request_id, n)
);
CREATE TABLE IF NOT EXISTS resolve.events (
    id         bigserial PRIMARY KEY,
    request_id text NOT NULL,
    kind       text NOT NULL,
    data       jsonb,
    at         timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""

TERMINAL = ("completed", "unresolved")


def j(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def row_dict(row: asyncpg.Record | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("question", "facts", "intent", "approved_by", "diff", "data"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    return d


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def open(self) -> Store:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return self

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def reset(self, request_id: str) -> None:
        async with self.pool.acquire() as c, c.transaction():
            for t in ("events", "turns", "versions"):
                await c.execute(f"DELETE FROM resolve.{t} WHERE request_id = $1", request_id)
            await c.execute("DELETE FROM resolve.requests WHERE request_id = $1", request_id)

    async def event(self, request_id: str, kind: str, **data) -> None:
        await self.pool.execute("INSERT INTO resolve.events (request_id, kind, data) VALUES ($1, $2, $3::jsonb)",
                                request_id, kind, j(data))

    async def create(self, request_id: str, customer_id: int, appointment_id: int, region: str, service: str,
                     today, horizon_days: int, facts: dict, confirm: bool = False) -> None:
        await self.pool.execute(
            "INSERT INTO resolve.requests (request_id, customer_id, appointment_id, region, service, today, "
            "horizon_days, state, facts, confirm) VALUES ($1, $2, $3, $4, $5, $6, $7, 'needs_input', $8::jsonb, $9)",
            request_id, customer_id, appointment_id, region, service, today, horizon_days, j(facts), confirm)

    async def get(self, request_id: str) -> dict | None:
        return row_dict(await self.pool.fetchrow("SELECT * FROM resolve.requests WHERE request_id = $1", request_id))

    async def add_turn(self, request_id: str, role: str, text: str, kind: str | None = None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO resolve.turns (request_id, n, role, text, kind) VALUES ($1, "
            "(SELECT coalesce(max(n), -1) + 1 FROM resolve.turns WHERE request_id = $1), $2, $3, $4) RETURNING n",
            request_id, role, text, kind)

    async def turns(self, request_id: str) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM resolve.turns WHERE request_id = $1 ORDER BY n", request_id)
        return [dict(r) for r in rows]

    async def versions(self, request_id: str) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM resolve.versions WHERE request_id = $1 ORDER BY version",
                                     request_id)
        return [row_dict(r) for r in rows]

    async def intent(self, request_id: str, version: int) -> dict:
        row = await self.pool.fetchrow("SELECT intent FROM resolve.versions WHERE request_id = $1 AND version = $2",
                                       request_id, version)
        return row_dict(row)["intent"]

    async def approve(self, request_id: str, intent: dict, approved_by: dict, diff: dict | None,
                      from_states=("needs_input", "ready")) -> int | None:
        """Store a new approved version and make it the one to execute. Refused (None) while the worker holds the
        request or after it ended: the version being executed is frozen."""
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("SELECT state, version FROM resolve.requests WHERE request_id = $1 FOR UPDATE",
                                   request_id)
            if row["state"] not in from_states:
                return None
            v = row["version"] + 1
            await c.execute("INSERT INTO resolve.versions (request_id, version, intent, approved_by, diff) "
                            "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)",
                            request_id, v, j(intent), j(approved_by), j(diff))
            await c.execute("UPDATE resolve.requests SET version = $2, state = 'ready', question = NULL, "
                            "updated_at = clock_timestamp() WHERE request_id = $1", request_id, v)
            return v

    async def set_state(self, request_id: str, state: str, *, owner: str | None = None, **fields) -> bool:
        """Move to `state`. With `owner`, only while that worker still holds the lease (a reclaimed lease wins)."""
        sets = ["state = $2", "updated_at = clock_timestamp()"]
        args = [request_id, state]
        for k, v in fields.items():
            args.append(j(v) if k == "question" else v)
            sets.append(f"{k} = ${len(args)}" + ("::jsonb" if k == "question" else ""))
        if state != "claimed":
            sets += ["lease_owner = NULL", "lease_until = NULL"]
        where = "request_id = $1"
        if owner is not None:
            args.append(owner)
            where += f" AND state = 'claimed' AND lease_owner = ${len(args)}"
        res = await self.pool.execute(f"UPDATE resolve.requests SET {', '.join(sets)} WHERE {where}", *args)
        return res.endswith(" 1")

    async def claim(self, owner: str, lease_s: float, request_id: str | None = None) -> dict | None:
        """Claim one ready request, or one whose worker's lease expired (it died mid-execution)."""
        extra = "AND request_id = $3" if request_id else ""
        args = [owner, lease_s] + ([request_id] if request_id else [])
        row = await self.pool.fetchrow(
            f"""
            UPDATE resolve.requests SET state = 'claimed', lease_owner = $1,
                   lease_until = clock_timestamp() + make_interval(secs => $2), updated_at = clock_timestamp()
            WHERE request_id = (
                SELECT request_id FROM resolve.requests
                WHERE (state = 'ready' OR (state = 'claimed' AND lease_until < clock_timestamp())) {extra}
                ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *
            """, *args)
        return row_dict(row)

    async def record_attempt(self, request_id: str, owner: str, slot_id: int | None, key: str | None,
                             state: str | None) -> bool:
        res = await self.pool.execute(
            "UPDATE resolve.requests SET attempt_slot = $3, attempt_key = $4, attempt_state = $5, "
            "updated_at = clock_timestamp() WHERE request_id = $1 AND state = 'claimed' AND lease_owner = $2",
            request_id, owner, slot_id, key, state)
        return res.endswith(" 1")
