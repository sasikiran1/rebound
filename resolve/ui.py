"""The Resolve service: the screen, read APIs over the store and the SQL oracle, and the customer-facing intake.

    uv run uvicorn resolve.ui:app --port 8090        then open http://127.0.0.1:8090/

Screen and reads: the conversation, what was understood (each approved version, its diff and who approved it), the
real alternatives behind an open question, the request state and the oracle's verdict computed from SQL (shown for
case fixtures only).

Intake (writes, model calls): POST /api/requests opens a request from a customer message, POST
/api/requests/{id}/messages adds a customer turn. They call the model named by RESOLVE_MODEL (default
muse-spark-1.3, billed) under per-process caps RESOLVE_MAX_CALLS / RESOLVE_MAX_COST, and RESOLVE_CONFIRM (default
true) makes every booking wait for an explicit yes. There is no authentication: bind to localhost only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from harness import settings
from resolve import intent as I
from resolve.api import ApiError, FieldDesk
from resolve.eval import cases as C
from resolve.eval.oracle import verdict
from resolve.evaluate import describe
from resolve.interpret import BudgetExceeded
from resolve.service import Resolve
from resolve.store import Store

PAGE = Path("ui/resolve.html")
state: dict = {}
TODAY = dt.date.fromisoformat(os.environ.get("RESOLVE_TODAY", C.TODAY.isoformat()))  # fixture clock by default
HORIZON_DAYS = int(os.environ.get("RESOLVE_HORIZON_DAYS", C.HORIZON_DAYS))


@asynccontextmanager
async def lifespan(_app):
    state["store"] = await Store(settings.admin_dsn(settings.TRIAL_DB)).open()
    state["oracle"] = await asyncpg.create_pool(settings.oracle_dsn(), min_size=1, max_size=2)
    state["cases"] = {c.id: c for split in ("dev", "heldout", "faults") for c in C.load(C.CASE_DIR / f"{split}.yaml")}
    state["api"] = FieldDesk(settings.FIELDDESK_URL, actor="resolve-service")
    state["model"] = None  # created on first intake call, so the screen needs no model key
    yield
    if state["model"] is not None and hasattr(state["model"].client, "close"):
        await state["model"].client.close()
    await state["api"].close()
    await state["store"].close()
    await state["oracle"].close()


app = FastAPI(title="Resolve", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def page():
    return FileResponse(PAGE)


@app.get("/healthz")
async def healthz():
    """Liveness plus the two dependencies the service needs: its store and FieldDesk."""
    out = {"ok": True}
    try:
        await state["store"].pool.fetchval("SELECT 1")
        out["store"] = "ok"
    except (OSError, asyncpg.PostgresError) as exc:
        out.update(ok=False, store=type(exc).__name__)
    try:
        await state["api"]._call("health", "GET", "/healthz")
        out["fielddesk"] = "ok"
    except ApiError as exc:
        out.update(ok=False, fielddesk=exc.status)
    if not out["ok"]:
        raise HTTPException(503, out)
    return out


@app.get("/api/metrics")
async def metrics():
    """Counts by state, questions asked, and the worker's outcomes, from the store (no model, no FieldDesk)."""
    pool = state["store"].pool
    by_state = {r["state"]: r["n"] for r in await pool.fetch(
        "SELECT state, count(*) AS n FROM resolve.requests GROUP BY state")}
    events = {r["kind"]: r["n"] for r in await pool.fetch(
        "SELECT kind, count(*) AS n FROM resolve.events GROUP BY kind")}
    questions = await pool.fetchval("SELECT coalesce(sum(questions_asked), 0) FROM resolve.requests")
    model = state["model"]
    return {"requests_by_state": by_state, "events": events, "questions_asked": questions,
            "model_calls_this_process": len(model.log.calls) if model else 0,
            "model_cost_usd_this_process": round(model.log.cost, 6) if model else 0.0}


def _service() -> Resolve:
    if state["model"] is None:
        from resolve.models import make_model
        state["model"] = make_model(os.environ.get("RESOLVE_MODEL", "muse-spark-1.3"),
                                    max_calls=int(os.environ.get("RESOLVE_MAX_CALLS", "50")),
                                    max_cost=float(os.environ.get("RESOLVE_MAX_COST", "1.00")))
    return Resolve(state["store"], state["api"], state["model"], TODAY, HORIZON_DAYS)


class Intake(BaseModel):
    customer_id: int
    appointment_id: int
    message: str = Field(min_length=1, max_length=2000)


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.post("/api/requests")
async def open_request(body: Intake):
    svc = _service()
    try:
        facts = await svc.load_facts(body.customer_id, body.appointment_id)
        ctx = await state["api"].context(body.customer_id)
    except (ApiError, StopIteration) as exc:
        raise HTTPException(422, f"cannot load this customer's appointment: {exc}") from exc
    appt = next(a for a in ctx["appointments"] if a["appointment_id"] == body.appointment_id)
    if appt["status"] != "scheduled":
        raise HTTPException(422, f"appointment {body.appointment_id} is {appt['status']}")
    rid = f"svc-{uuid.uuid4().hex[:12]}"
    try:
        res = await svc.open(rid, body.customer_id, body.appointment_id, ctx["region"], appt["service"],
                             body.message, facts, confirm=os.environ.get("RESOLVE_CONFIRM", "true") == "true")
    except BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    return {"request_id": rid, **res}


@app.post("/api/requests/{rid}/messages")
async def customer_message(rid: str, body: Message):
    if await state["store"].get(rid) is None:
        raise HTTPException(404, "no such request")
    try:
        return await _service().customer_says(rid, body.text)
    except BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc


@app.get("/api/requests")
async def requests(limit: int = 40):
    rows = await state["store"].pool.fetch(
        "SELECT request_id, state, version, updated_at FROM resolve.requests ORDER BY updated_at DESC LIMIT $1", limit)
    return [{**dict(r), "updated_at": r["updated_at"].isoformat()} for r in rows]


def _turn_times(rid: str) -> tuple[list[dt.datetime] | None, str]:
    """Harness-recorded customer turn times (demo or evaluation record), if there is one for this request."""
    run = rid.split(":")[0]
    for path in (Path("runs/resolve/demo") / f"{run}.json",):
        if path.exists():
            d = json.loads(path.read_text())
            if d.get("request_id") == rid and d.get("turn_times"):
                return [dt.datetime.fromisoformat(t) for t in d["turn_times"]], "harness"
    return None, "store"


@app.get("/api/request")
async def request(rid: str):
    store: Store = state["store"]
    req = await store.get(rid)
    if req is None:
        return {"error": "not found"}
    names = {t["technician_id"]: t["name"] for t in req["facts"]["technicians"]}
    turns = await store.turns(rid)
    versions = await store.versions(rid)
    ev = await store.pool.fetch("SELECT kind, data, at FROM resolve.events WHERE request_id = $1 ORDER BY id", rid)
    events = [{"kind": e["kind"], "data": json.loads(e["data"]), "at": e["at"].isoformat()} for e in ev]
    question = req["question"]
    if question:
        question = {**question, "candidates": [{**c, "label": describe(c, names)} for c in question["candidates"]]}
    out = {
        "request_id": rid, "state": req["state"], "version": req["version"], "questions_asked": req["questions_asked"],
        "detail": req["detail"], "question": question, "booked_slot": req["booked_slot"],
        "attempt": {"slot": req["attempt_slot"], "state": req["attempt_state"]},
        "lease": {"owner": req["lease_owner"], "until": req["lease_until"] and req["lease_until"].isoformat()},
        "customer": req["facts"]["customer"], "appointment": req["facts"]["appointment_being_moved"],
        "turns": [{"role": t["role"], "kind": t["kind"], "text": t["text"], "at": t["created_at"].isoformat()}
                  for t in turns],
        "versions": [{"version": v["version"], "approved_by": v["approved_by"], "diff": v["diff"],
                      "summary": I.explain(v["intent"], names),
                      "rules": [{"id": r["id"], "text": I.explain_rule(r, names, strength=False),
                                 "strength": r["strength"],
                                 "quote": r["quote"], "where": loc[0] if loc[1] is None else f"{loc[0]} {loc[1]}"}
                                for r, loc in I.all_rules(v["intent"])],
                      "unclear": [u["phrase"] for u in v["intent"].get("unclear", [])],
                      "approved_slots": v["intent"].get("approved_slots", [])} for v in versions],
        "events": events,
    }
    case_id = rid.split(":")[1] if rid.count(":") >= 2 else None
    case = state["cases"].get(case_id)
    if case is not None:
        async with state["oracle"].acquire() as conn:
            times, source = _turn_times(rid)
            if times is None:
                times = [t["created_at"] for t in turns if t["role"] == "customer"]
            v = await verdict(conn, case, times)
            rows = await conn.fetch("SELECT id, to_slot_id, idempotency_key, applied_at FROM appointment_changes "
                                    "WHERE appointment_id = $1 ORDER BY id", case.appointment_id)
        out["sql"] = {"verdict": v, "turn_times_from": source, "case": case.id, "synthetic": True,
                      "expected_final": case.expected["final"],
                      "changes": [{"id": r["id"], "to": case.alias_of(r["to_slot_id"]), "key": r["idempotency_key"],
                                   "applied_at": r["applied_at"].isoformat()} for r in rows]}
    return out
