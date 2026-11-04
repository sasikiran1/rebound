"""Durable Resolve state in PostgreSQL (schema `resolve` in the trial database, direct port, not through Toxiproxy:
it stands for the assistant's own store, which the FieldDesk outage does not affect).

A request has one stable identity (`request_id`) and moves through explicit states:

  needs_input  waiting for the customer (a clarification or a permission question is open)
  ready        an approved intent version waits for the worker
  claimed      the worker holds a lease and is executing the frozen version
  completed    the booking is confirmed (change_id recorded)
  unresolved   nothing will be booked (declined, unsupported, or no progress)
With `confirm` set (product mode), the worker never books a slot the customer has not explicitly approved: it asks
"shall I book <slot>?" first. The frozen evaluation runs with it off, as the cases were written.

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
-- Backoff: a released request is not claimed by the service loop before not_before (Retry-After or exponential).
ALTER TABLE resolve.requests ADD COLUMN IF NOT EXISTS not_before timestamptz;
ALTER TABLE resolve.requests ADD COLUMN IF NOT EXISTS retries integer NOT NULL DEFAULT 0;
-- Fencing: every claim increments the generation; a worker's writes count only with the generation it claimed.
ALTER TABLE resolve.requests ADD COLUMN IF NOT EXISTS lease_gen integer NOT NULL DEFAULT 0;
-- A correction (or a close) that arrived while an earlier write was unsettled waits here until the worker has
-- settled that write: it takes effect only after the old write can no longer land.
ALTER TABLE resolve.requests ADD COLUMN IF NOT EXISTS pending_version integer;
ALTER TABLE resolve.requests ADD COLUMN IF NOT EXISTS pending_close jsonb;
-- FieldDesk key fencing (see backend/fielddesk/app.py): lets the worker void a superseded, unconfirmed write.
CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key     text PRIMARY KEY,
    outcome text NOT NULL CHECK (outcome IN ('applied', 'voided')),
    at      timestamptz NOT NULL DEFAULT clock_timestamp()
);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fielddesk') THEN
        GRANT SELECT, INSERT ON public.idempotency_keys TO fielddesk;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_ro') THEN
        GRANT SELECT ON public.idempotency_keys TO oracle_ro;
    END IF;
END $$;
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
CREATE TABLE IF NOT EXISTS resolve.notifications (
    id              bigserial PRIMARY KEY,
    request_id      text    NOT NULL REFERENCES resolve.requests (request_id),
    turn_n          integer NOT NULL,
    kind            text    NOT NULL,
    recipient       text    NOT NULL,
    subject         text    NOT NULL,
    body            text    NOT NULL,
    status          text    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error      text,
    sent_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (request_id, turn_n)
);
-- One row per completed request: its appointment mirrored into the technicians' CalDAV calendars.
CREATE TABLE IF NOT EXISTS resolve.calendar_sync (
    request_id      text    PRIMARY KEY REFERENCES resolve.requests (request_id),
    status          text    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'synced', 'failed')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error      text,
    event_url       text,
    synced_at       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp()
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
# Assistant turns the customer must receive, and the subject line of their message. Recipients are synthetic
# (customer-<id>@example.test): FieldDesk stores no contact details.
NOTIFY = {
    "permission": "Your appointment: a question before we change it",
    "clarify": "Your appointment: which did you mean?",
    "no_options": "Your appointment: nothing open fits",
    "horizon": "Your appointment: that date is too far ahead",
    "confirm": "Your appointment: please confirm the new time",
    "repeat": "Your appointment: could you say that another way?",
    "completed": "Your appointment has been moved",
    "unresolved": "Your appointment has not been changed",
}


def j(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def row_dict(row: asyncpg.Record | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("question", "facts", "intent", "approved_by", "diff", "data", "pending_close"):
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
            for t in ("events", "notifications", "calendar_sync", "turns", "versions"):
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
        """Append a turn. An assistant turn the customer must see (a question, a confirmation, an outcome) also
        queues its notification in the same transaction, so a question never exists without its message."""
        async with self.pool.acquire() as c, c.transaction():
            return await self._turn(c, request_id, role, text, kind)

    @staticmethod
    async def _turn(c, request_id: str, role: str, text: str, kind: str | None) -> int:
        n = await c.fetchval(
            "INSERT INTO resolve.turns (request_id, n, role, text, kind) VALUES ($1, "
            "(SELECT coalesce(max(n), -1) + 1 FROM resolve.turns WHERE request_id = $1), $2, $3, $4) RETURNING n",
            request_id, role, text, kind)
        if role == "assistant" and kind in NOTIFY:
            customer = await c.fetchval("SELECT customer_id FROM resolve.requests WHERE request_id = $1", request_id)
            await c.execute(
                "INSERT INTO resolve.notifications (request_id, turn_n, kind, recipient, subject, body) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                request_id, n, kind, f"customer-{customer}@example.test", NOTIFY[kind], text)
        return n

    async def notifications(self, request_id: str) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM resolve.notifications WHERE request_id = $1 ORDER BY id",
                                     request_id)
        return [dict(r) for r in rows]

    async def calendar_sync(self, request_id: str) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM resolve.calendar_sync WHERE request_id = $1", request_id)
        return dict(row) if row else None

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
        request or after it ended: the version being executed is frozen. While an earlier write is unsettled
        (attempt `sending`), the version is stored as *pending*: the worker settles that write first and only then
        makes the new version current, so the old write cannot commit after the new version took effect."""
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("SELECT state, version, pending_version, attempt_state FROM resolve.requests "
                                   "WHERE request_id = $1 FOR UPDATE", request_id)
            if row["state"] not in from_states:
                return None
            v = max(row["version"], row["pending_version"] or 0) + 1
            await c.execute("INSERT INTO resolve.versions (request_id, version, intent, approved_by, diff) "
                            "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)",
                            request_id, v, j(intent), j(approved_by), j(diff))
            target = "pending_version" if row["attempt_state"] == "sending" else "version"
            await c.execute(f"UPDATE resolve.requests SET {target} = $2, state = 'ready', question = NULL, "
                            "not_before = NULL, retries = 0, updated_at = clock_timestamp() WHERE request_id = $1",
                            request_id, v)
            return v

    async def clear_attempt(self, request_id: str, key: str) -> bool:
        """Forget a settled attempt (outside a worker's lease: the conversation settled it)."""
        res = await self.pool.execute(
            "UPDATE resolve.requests SET attempt_slot = NULL, attempt_key = NULL, attempt_state = NULL, "
            "updated_at = clock_timestamp() WHERE request_id = $1 AND attempt_key = $2 AND state <> 'claimed'",
            request_id, key)
        return res.endswith(" 1")

    async def promote(self, request_id: str, owner: str, gen: int) -> int | None:
        """Make the pending version current (the worker has settled the earlier write)."""
        return await self.pool.fetchval(
            "UPDATE resolve.requests SET version = pending_version, pending_version = NULL, "
            "updated_at = clock_timestamp() WHERE request_id = $1 AND state = 'claimed' AND lease_owner = $2 "
            "AND lease_gen = $3 AND pending_version IS NOT NULL RETURNING version", request_id, owner, gen)

    async def close_pending(self, request_id: str, detail: str, text: str, text_if_applied: str) -> None:
        """The request should end (declined, or not interpretable), but an earlier write is unsettled: hand it to the
        worker, which settles the write and then closes the request: `text` if the write was voided, or what happened
        followed by `text_if_applied` if it had gone through."""
        await self.pool.execute(
            "UPDATE resolve.requests SET pending_close = $2::jsonb, state = 'ready', question = NULL, "
            "not_before = NULL, updated_at = clock_timestamp() WHERE request_id = $1",
            request_id, j({"detail": detail, "text": text, "text_if_applied": text_if_applied}))

    async def set_state(self, request_id: str, state: str, *, owner: str | None = None, gen: int | None = None,
                        turn: tuple[str, str] | None = None, delay_s: float | None = None, **fields) -> bool:
        """Move to `state`. With `owner` (and `gen`), only while that worker still holds that lease: a reclaimed
        lease wins. `turn` = (text, kind): the assistant turn (and its notification) is written in the same
        transaction, so a question or an outcome never exists without its message. `delay_s`: the service loop does
        not claim the request again before then. Completing a request also queues its calendar sync."""
        sets = ["state = $2", "updated_at = clock_timestamp()"]
        args = [request_id, state]
        for k, v in fields.items():
            args.append(j(v) if k == "question" else v)
            sets.append(f"{k} = ${len(args)}" + ("::jsonb" if k == "question" else ""))
        if delay_s is not None:
            args.append(float(delay_s))
            sets += [f"not_before = clock_timestamp() + make_interval(secs => ${len(args)})", "retries = retries + 1"]
        elif state != "claimed":
            sets += ["not_before = NULL", "retries = 0"]
        if state != "claimed":
            sets += ["lease_owner = NULL", "lease_until = NULL"]
        where = "request_id = $1"
        if owner is not None:
            args.append(owner)
            where += f" AND state = 'claimed' AND lease_owner = ${len(args)}"
        if gen is not None:
            args.append(gen)
            where += f" AND lease_gen = ${len(args)}"
        async with self.pool.acquire() as c, c.transaction():
            res = await c.execute(f"UPDATE resolve.requests SET {', '.join(sets)} WHERE {where}", *args)
            ok = res.endswith(" 1")
            if ok and turn is not None:
                await self._turn(c, request_id, "assistant", turn[0], turn[1])
            if state == "completed" and ok:
                await c.execute("INSERT INTO resolve.calendar_sync (request_id) VALUES ($1) ON CONFLICT DO NOTHING",
                                request_id)
        return ok

    async def claim(self, owner: str, lease_s: float, request_id: str | None = None,
                    honor_backoff: bool = True) -> dict | None:
        """Claim one ready request, or one whose worker's lease expired (it died mid-execution). Each claim starts a
        new lease generation. The service loop honors `not_before`; a run aimed at one request (a check, the demo)
        may pass honor_backoff=False and paces itself."""
        extra = "AND request_id = $3" if request_id else ""
        wait = "AND (not_before IS NULL OR not_before <= clock_timestamp())" if honor_backoff else ""
        args = [owner, lease_s] + ([request_id] if request_id else [])
        row = await self.pool.fetchrow(
            f"""
            UPDATE resolve.requests SET state = 'claimed', lease_owner = $1, lease_gen = lease_gen + 1,
                   lease_until = clock_timestamp() + make_interval(secs => $2), updated_at = clock_timestamp()
            WHERE request_id = (
                SELECT request_id FROM resolve.requests
                WHERE ((state = 'ready' {wait}) OR (state = 'claimed' AND lease_until < clock_timestamp())) {extra}
                ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *
            """, *args)
        return row_dict(row)

    async def renew(self, request_id: str, owner: str, gen: int, lease_s: float) -> bool:
        """Extend a lease that is still held and unexpired; False means another worker may own the request now."""
        res = await self.pool.execute(
            "UPDATE resolve.requests SET lease_until = clock_timestamp() + make_interval(secs => $4) "
            "WHERE request_id = $1 AND state = 'claimed' AND lease_owner = $2 AND lease_gen = $3 "
            "AND lease_until > clock_timestamp()", request_id, owner, gen, lease_s)
        return res.endswith(" 1")

    async def record_attempt(self, request_id: str, owner: str, slot_id: int | None, key: str | None,
                             state: str | None, gen: int | None = None) -> bool:
        res = await self.pool.execute(
            "UPDATE resolve.requests SET attempt_slot = $3, attempt_key = $4, attempt_state = $5, "
            "updated_at = clock_timestamp() WHERE request_id = $1 AND state = 'claimed' AND lease_owner = $2 "
            "AND ($6::integer IS NULL OR lease_gen = $6)",
            request_id, owner, slot_id, key, state, gen)
        return res.endswith(" 1")
