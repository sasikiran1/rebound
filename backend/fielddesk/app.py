"""FieldDesk: a field-service scheduling API (the system under test).

Endpoint cost comes only from real queries on generated data. Errors use one JSON shape:
{"error": {"type": "...", "message": "..."}}.
"""

from __future__ import annotations

import datetime as dt
import os
from contextlib import asynccontextmanager
from typing import Annotated

import asyncpg
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fielddesk.db import db
from fielddesk.gate import Gate, GateMiddleware
from fielddesk.requestlog import RequestLogMiddleware, counters
from fielddesk.sampler import Sampler

SLOT_CONFLICT = "appointments_one_scheduled_per_slot"
KEY_CONFLICT = "appointment_changes_idempotency_key"


class ApiError(Exception):
    def __init__(self, status: int, type_: str, message: str):
        self.status, self.type, self.message = status, type_, message


def gate_from_env() -> Gate | None:
    """Read once at startup; FieldDesk restarts before every trial."""
    if os.environ.get("GATE_ENABLED", "false").lower() != "true":
        return None
    return Gate(
        tokens=int(os.environ.get("GATE_TOKENS", str(db.size))),
        queue_max=int(os.environ.get("GATE_QUEUE_MAX", str(2 * db.size))),
        queue_wait_s=float(os.environ.get("GATE_QUEUE_WAIT_MS", "1500")) / 1000,
    )


gate = gate_from_env()
sampler = Sampler(os.environ.get("SAMPLE_LOG"), counters, db, gate)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.open()
    sampler.start()
    yield
    sampler.stop()
    await db.close()


app = FastAPI(title="FieldDesk", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Starlette runs the last-added middleware first: request log (outermost) -> gate -> routes.
app.add_middleware(GateMiddleware, gate=gate, retry_after_s=int(os.environ.get("GATE_RETRY_AFTER_S", "2")))
app.add_middleware(RequestLogMiddleware, path=os.environ.get("REQUEST_LOG"))


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError):
    return JSONResponse({"error": {"type": exc.type, "message": exc.message}}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError):
    problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse({"error": {"type": "invalid_request", "message": problems[:300]}}, status_code=422)


def slot_json(row) -> dict:
    return {
        "slot_id": row["slot_id"],
        "date": row["slot_date"].isoformat(),
        "starts_at": row["starts_at"].isoformat(timespec="minutes"),
        "technician_id": row["technician_id"],
    }


def appointment_json(row) -> dict:
    return {
        "appointment_id": row["id"],
        "customer_id": row["customer_id"],
        "status": row["status"],
        "service": row["service"],
        "required_skill": row["skill"],
        **slot_json(row),
    }


APPOINTMENT_SELECT = """
    SELECT a.id, a.customer_id, a.status, st.code AS service, sk.code AS skill, a.slot_id,
           s.slot_date, s.starts_at, s.technician_id
    FROM appointments a
    JOIN slots s ON s.id = a.slot_id
    JOIN service_types st ON st.id = a.service_type_id
    JOIN skills sk ON sk.id = st.skill_id
"""


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/customers/{customer_id}/appointments")
async def customer_appointments(customer_id: int):
    async with db.acquire() as conn:
        customer = await conn.fetchrow(
            "SELECT c.id, r.code AS region FROM customers c JOIN regions r ON r.id = c.region_id WHERE c.id = $1",
            customer_id,
        )
        if customer is None:
            raise ApiError(404, "not_found", f"customer {customer_id} not found")
        rows = await conn.fetch(
            APPOINTMENT_SELECT + " WHERE a.customer_id = $1 ORDER BY s.starts_at, a.id", customer_id
        )
    return {
        "customer_id": customer_id,
        "region": customer["region"],
        "appointments": [appointment_json(r) for r in rows],
    }


@app.get("/appointments/{appointment_id}")
async def get_appointment(appointment_id: int):
    async with db.acquire() as conn:
        row = await conn.fetchrow(APPOINTMENT_SELECT + " WHERE a.id = $1", appointment_id)
    if row is None:
        raise ApiError(404, "not_found", f"appointment {appointment_id} not found")
    return appointment_json(row)


def in_window(starts_at: dt.datetime, start_after: dt.time | None, start_before: dt.time | None) -> bool:
    t = starts_at.time()
    return (start_after is None or t >= start_after) and (start_before is None or t < start_before)


@app.get("/availability")
async def day_availability(region: str, date: dt.date, service: str, start_after: dt.time | None = None,
                           start_before: dt.time | None = None, offset: Annotated[int, Query(ge=0)] = 0):
    """`start_after`, `start_before` (local HH:MM) and `offset` are optional (tool set v2). They filter and page the
    same query's rows; with none of them the response is exactly the v1 response."""
    async with db.acquire() as conn:
        ids = await conn.fetchrow(
            "SELECT (SELECT id FROM regions WHERE code = $1) AS region_id, "
            "(SELECT skill_id FROM service_types WHERE code = $2) AS skill_id",
            region, service,
        )
        if ids["region_id"] is None or ids["skill_id"] is None:
            raise ApiError(422, "unknown_code", f"unknown region {region!r} or service {service!r}")
        rows = await conn.fetch(
            """
            SELECT s.id AS slot_id, s.slot_date, s.starts_at, s.technician_id
            FROM slots s
            JOIN technician_regions tr ON tr.technician_id = s.technician_id AND tr.region_id = $1
            JOIN technician_skills ts ON ts.technician_id = s.technician_id AND ts.skill_id = $2
            WHERE s.slot_date = $3
              AND NOT EXISTS (
                  SELECT 1 FROM appointments a WHERE a.slot_id = s.id AND a.status = 'scheduled')
            ORDER BY s.starts_at, s.technician_id
            """,
            ids["region_id"], ids["skill_id"], date,
        )
    if start_after is None and start_before is None and offset == 0:
        return {
            "region": region,
            "date": date.isoformat(),
            "service": service,
            "count": len(rows),
            "slots": [slot_json(r) for r in rows],
        }
    rows = [r for r in rows if in_window(r["starts_at"], start_after, start_before)]
    return {
        "region": region,
        "date": date.isoformat(),
        "service": service,
        "start_after": start_after and start_after.isoformat("minutes"),
        "start_before": start_before and start_before.isoformat("minutes"),
        "count": len(rows),
        "offset": offset,
        "slots": [slot_json(r) for r in rows[offset:]],
    }


SEARCH_LIMIT = 50
MAX_SEARCH_DAYS = 21


@app.get("/availability/search")
async def search_availability(
    service: str,
    date_from: Annotated[dt.date, Query(alias="from")],
    date_to: Annotated[dt.date, Query(alias="to")],
    regions: Annotated[str, Query(description="comma-separated region codes; the first one ranks by distance")],
    start_after: dt.time | None = None,
    start_before: dt.time | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Open slots over a date range for technicians covering any of the regions, ranked by distance from the
    first region. Multi-day, multi-region join: the expensive path. Optional (tool set v2): `start_after`,
    `start_before` (local HH:MM) filter by start time inside the query; `offset` pages past the first 50."""
    codes = list(dict.fromkeys(c.strip() for c in regions.split(",") if c.strip()))
    if not codes:
        raise ApiError(422, "invalid_regions", "at least one region code is required")
    if date_to < date_from or (date_to - date_from).days >= MAX_SEARCH_DAYS:
        raise ApiError(422, "invalid_range", f"need from <= to and at most {MAX_SEARCH_DAYS} days")
    async with db.acquire() as conn:
        ids = await conn.fetchrow(
            "SELECT (SELECT skill_id FROM service_types WHERE code = $1) AS skill_id, "
            "(SELECT array_agg(id ORDER BY array_position($2::text[], code)) FROM regions "
            " WHERE code = ANY($2::text[])) AS region_ids",
            service, codes,
        )
        if ids["skill_id"] is None or ids["region_ids"] is None or len(ids["region_ids"]) != len(codes):
            raise ApiError(422, "unknown_code", f"unknown service {service!r} or region in {codes}")
        rows = await conn.fetch(
            """
            SELECT s.id AS slot_id, s.slot_date, s.starts_at, s.technician_id, hr.code AS technician_home_region,
                   min(rd.distance) AS distance, array_agg(DISTINCT r.code ORDER BY r.code) AS covers,
                   count(*) OVER () AS total
            FROM slots s
            JOIN technician_skills ts ON ts.technician_id = s.technician_id AND ts.skill_id = $1
            JOIN technician_regions tr ON tr.technician_id = s.technician_id AND tr.region_id = ANY($2::smallint[])
            JOIN regions r ON r.id = tr.region_id
            JOIN region_distances rd ON rd.from_region = $3 AND rd.to_region = tr.region_id
            JOIN technicians t ON t.id = s.technician_id
            JOIN regions hr ON hr.id = t.home_region
            WHERE s.slot_date BETWEEN $4 AND $5
              AND NOT EXISTS (
                  SELECT 1 FROM appointments a WHERE a.slot_id = s.id AND a.status = 'scheduled')
            GROUP BY s.id, s.slot_date, s.starts_at, s.technician_id, hr.code
            ORDER BY distance, s.slot_date, s.starts_at, s.technician_id
            LIMIT $6
            """ if start_after is None and start_before is None and offset == 0 else """
            SELECT s.id AS slot_id, s.slot_date, s.starts_at, s.technician_id, hr.code AS technician_home_region,
                   min(rd.distance) AS distance, array_agg(DISTINCT r.code ORDER BY r.code) AS covers,
                   count(*) OVER () AS total
            FROM slots s
            JOIN technician_skills ts ON ts.technician_id = s.technician_id AND ts.skill_id = $1
            JOIN technician_regions tr ON tr.technician_id = s.technician_id AND tr.region_id = ANY($2::smallint[])
            JOIN regions r ON r.id = tr.region_id
            JOIN region_distances rd ON rd.from_region = $3 AND rd.to_region = tr.region_id
            JOIN technicians t ON t.id = s.technician_id
            JOIN regions hr ON hr.id = t.home_region
            WHERE s.slot_date BETWEEN $4 AND $5
              AND ($7::time IS NULL OR s.starts_at::time >= $7) AND ($8::time IS NULL OR s.starts_at::time < $8)
              AND NOT EXISTS (
                  SELECT 1 FROM appointments a WHERE a.slot_id = s.id AND a.status = 'scheduled')
            GROUP BY s.id, s.slot_date, s.starts_at, s.technician_id, hr.code
            ORDER BY distance, s.slot_date, s.starts_at, s.technician_id
            LIMIT $6 OFFSET $9
            """,
            *((ids["skill_id"], ids["region_ids"], ids["region_ids"][0], date_from, date_to, SEARCH_LIMIT)
              + (() if start_after is None and start_before is None and offset == 0
                 else (start_after, start_before, offset))),
        )
    if start_after is not None or start_before is not None or offset:
        return {
            "service": service, "from": date_from.isoformat(), "to": date_to.isoformat(), "regions": codes,
            "start_after": start_after and start_after.isoformat("minutes"),
            "start_before": start_before and start_before.isoformat("minutes"),
            "total_count": rows[0]["total"] if rows else 0, "offset": offset,
            "slots": [{**slot_json(r), "technician_home_region": r["technician_home_region"],
                       "distance": r["distance"], "covers_regions": list(r["covers"])} for r in rows],
        }
    return {
        "service": service,
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "regions": codes,
        "total_count": rows[0]["total"] if rows else 0,
        "slots": [
            {**slot_json(r), "technician_home_region": r["technician_home_region"], "distance": r["distance"],
             "covers_regions": list(r["covers"])}
            for r in rows
        ],
    }


@app.get("/technicians")
async def list_technicians(region: str, skill: str):
    async with db.acquire() as conn:
        ids = await conn.fetchrow(
            "SELECT (SELECT id FROM regions WHERE code = $1) AS region_id, "
            "(SELECT id FROM skills WHERE code = $2) AS skill_id",
            region, skill,
        )
        if ids["region_id"] is None or ids["skill_id"] is None:
            raise ApiError(422, "unknown_code", f"unknown region {region!r} or skill {skill!r}")
        rows = await conn.fetch(
            """
            SELECT t.id, hr.code AS home_region,
                   (SELECT array_agg(sk.code ORDER BY sk.code) FROM technician_skills x
                    JOIN skills sk ON sk.id = x.skill_id WHERE x.technician_id = t.id) AS skills,
                   (SELECT array_agg(rg.code ORDER BY rg.code) FROM technician_regions y
                    JOIN regions rg ON rg.id = y.region_id WHERE y.technician_id = t.id) AS covers,
                   (SELECT count(*) FROM slots s WHERE s.technician_id = t.id AND NOT EXISTS (
                        SELECT 1 FROM appointments a WHERE a.slot_id = s.id AND a.status = 'scheduled'))
                       AS open_slots
            FROM technicians t
            JOIN technician_regions tr ON tr.technician_id = t.id AND tr.region_id = $1
            JOIN technician_skills ts ON ts.technician_id = t.id AND ts.skill_id = $2
            JOIN regions hr ON hr.id = t.home_region
            ORDER BY t.id
            """,
            ids["region_id"], ids["skill_id"],
        )
    return {
        "region": region,
        "skill": skill,
        "count": len(rows),
        "technicians": [
            {"technician_id": r["id"], "home_region": r["home_region"], "skills": list(r["skills"]),
             "covers_regions": list(r["covers"]), "open_slots_in_horizon": r["open_slots"]}
            for r in rows
        ],
    }


class RescheduleBody(BaseModel):
    slot_id: int
    customer_id: int


def change_json(row, replayed: bool) -> dict:
    return {
        "appointment_id": row["appointment_id"],
        "from_slot_id": row["from_slot_id"],
        "to_slot_id": row["to_slot_id"],
        "change_id": row["id"],
        "applied_at": row["applied_at"].isoformat(),
        "replayed": replayed,
    }


CHANGE_BY_KEY = (
    "SELECT id, appointment_id, from_slot_id, to_slot_id, applied_at "
    "FROM appointment_changes WHERE idempotency_key = $1"
)


@app.post("/appointments/{appointment_id}/reschedule")
async def reschedule(
    appointment_id: int,
    body: RescheduleBody,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """One transaction. A repeated idempotency key returns the original change and writes nothing."""
    try:
        async with db.acquire() as conn:
            async with conn.transaction():
                prior = await conn.fetchrow(CHANGE_BY_KEY, idempotency_key)
                if prior is not None:
                    return change_json(prior, replayed=True)
                appt = await conn.fetchrow(
                    """
                    SELECT a.id, a.customer_id, a.status, a.slot_id, st.skill_id, c.region_id
                    FROM appointments a
                    JOIN service_types st ON st.id = a.service_type_id
                    JOIN customers c ON c.id = a.customer_id
                    WHERE a.id = $1
                    FOR UPDATE OF a
                    """,
                    appointment_id,
                )
                if appt is None:
                    raise ApiError(404, "not_found", f"appointment {appointment_id} not found")
                if appt["customer_id"] != body.customer_id:
                    raise ApiError(403, "wrong_customer", "appointment does not belong to this customer")
                if appt["status"] != "scheduled":
                    raise ApiError(422, "not_scheduled", f"appointment is {appt['status']}")
                if appt["slot_id"] == body.slot_id:
                    raise ApiError(422, "same_slot", "appointment is already in this slot")
                slot = await conn.fetchrow(
                    """
                    SELECT s.id,
                           EXISTS (SELECT 1 FROM technician_skills ts
                                   WHERE ts.technician_id = s.technician_id AND ts.skill_id = $2) AS has_skill,
                           EXISTS (SELECT 1 FROM technician_regions tr
                                   WHERE tr.technician_id = s.technician_id AND tr.region_id = $3) AS covers
                    FROM slots s WHERE s.id = $1
                    """,
                    body.slot_id, appt["skill_id"], appt["region_id"],
                )
                if slot is None:
                    raise ApiError(404, "not_found", f"slot {body.slot_id} not found")
                if not slot["has_skill"] or not slot["covers"]:
                    raise ApiError(
                        422, "unqualified_slot",
                        "slot technician lacks the service skill or does not cover the customer's region",
                    )
                await conn.execute("UPDATE appointments SET slot_id = $2 WHERE id = $1", appointment_id, body.slot_id)
                change = await conn.fetchrow(
                    "INSERT INTO appointment_changes (appointment_id, from_slot_id, to_slot_id, idempotency_key) "
                    "VALUES ($1, $2, $3, $4) RETURNING id, appointment_id, from_slot_id, to_slot_id, applied_at",
                    appointment_id, appt["slot_id"], body.slot_id, idempotency_key,
                )
                return change_json(change, replayed=False)
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == SLOT_CONFLICT:
            raise ApiError(409, "slot_taken", f"slot {body.slot_id} is already booked") from exc
        if exc.constraint_name == KEY_CONFLICT:
            # A concurrent request with the same key committed first; return its result.
            async with db.acquire() as conn:
                prior = await conn.fetchrow(CHANGE_BY_KEY, idempotency_key)
            return change_json(prior, replayed=True)
        raise
