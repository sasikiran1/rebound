"""The Resolve screen: one page over the durable store and the independent SQL oracle.

    uv run uvicorn resolve.ui:app --port 8090        then open http://127.0.0.1:8090/

Read-only. It shows the conversation, what was understood (each approved version, its diff and who approved it),
the real alternatives behind an open question, the request state and the oracle's verdict computed from SQL.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.responses import FileResponse

from harness import settings
from resolve import intent as I
from resolve.eval import cases as C
from resolve.eval.oracle import verdict
from resolve.evaluate import describe
from resolve.store import Store

PAGE = Path("ui/resolve.html")
state: dict = {}


@asynccontextmanager
async def lifespan(_app):
    state["store"] = await Store(settings.admin_dsn(settings.TRIAL_DB)).open()
    state["oracle"] = await asyncpg.create_pool(settings.oracle_dsn(), min_size=1, max_size=2)
    state["cases"] = {c.id: c for split in ("dev", "heldout", "faults") for c in C.load(C.CASE_DIR / f"{split}.yaml")}
    yield
    await state["store"].close()
    await state["oracle"].close()


app = FastAPI(title="Resolve", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def page():
    return FileResponse(PAGE)


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
