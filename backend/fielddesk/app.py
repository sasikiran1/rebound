"""FieldDesk: a field-service scheduling API (the system under test).

Endpoint cost comes only from real queries on generated data. Errors use one JSON shape:
{"error": {"type": "...", "message": "..."}}.
"""

from __future__ import annotations

import datetime as dt
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fielddesk.db import db
from fielddesk.requestlog import RequestLogMiddleware

SLOT_CONFLICT = "appointments_one_scheduled_per_slot"
KEY_CONFLICT = "appointment_changes_idempotency_key"


class ApiError(Exception):
    def __init__(self, status: int, type_: str, message: str):
        self.status, self.type, self.message = status, type_, message


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.environ.get("GATE_ENABLED", "false").lower() == "true":
        raise RuntimeError("GATE_ENABLED=true but the admission gate is not implemented yet")
    await db.open()
    yield
    await db.close()


app = FastAPI(title="FieldDesk", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(RequestLogMiddleware, path=os.environ.get("REQUEST_LOG"))


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError):
    return JSONResponse({"error": {"type": exc.type, "message": exc.message}}, status_code=exc.status)


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
        **slot_json(row),
    }


APPOINTMENT_SELECT = """
    SELECT a.id, a.customer_id, a.status, st.code AS service, a.slot_id,
           s.slot_date, s.starts_at, s.technician_id
    FROM appointments a
    JOIN slots s ON s.id = a.slot_id
    JOIN service_types st ON st.id = a.service_type_id
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


@app.get("/availability")
async def day_availability(region: str, date: dt.date, service: str):
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
    return {
        "region": region,
        "date": date.isoformat(),
        "service": service,
        "count": len(rows),
        "slots": [slot_json(r) for r in rows],
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
