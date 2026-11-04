"""Live flow screen: the live console (resolve/eval/live.py) behind a browser page.

    scripts/resolve_live_ui.sh [--model muse-spark-1.3] [--port 8092]     then open http://127.0.0.1:8092/

Everything on the page is read from the running system: the request store (conversation, approved versions with
their quotes, worker events), FieldDesk's database (the technicians' slots and who holds each one), the technicians'
CalDAV calendars, Toxiproxy (the outage switch) and the SQL oracle's verdict against the case file. Nothing is
animated from a script: a step lights up when its row or event exists.

Controls map one to one onto the console's commands: lane (re-seeds the case's world), customer message, outage on/off,
another customer takes the contested slot, and a worker process that dies right after its booking POST.
Model calls are capped per process (RESOLVE_LIVE_MAX_CALLS, RESOLVE_LIVE_MAX_COST). No authentication: localhost only.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from resolve import app as A
from resolve.eval import cases as C
from resolve.eval.live import OUTAGE, Live, slot_label

PAGE = Path("ui/live.html")
SPLITS = ("dev", "heldout", "faults")
# Presets: real case files, chosen because each shows one way a reading can go wrong. `plan` is what Play script
# does besides sending the case file's lines: faults to inject, and the customer's correction when a proposed slot is
# not one the case file authorizes (a customer says no to the wrong slot; Play never says yes to it).
PRESETS = [
    {"case": "D1", "title": "A: ambiguous request + outage + slot taken + worker crash",
     "how": "Play: outage on, the message, another customer takes Fri 15:30 Dana while it waits, outage off; it asks "
            "before next week, gets the answer, proposes, and the worker is killed right after the booking write.",
     "plan": {"outage": True, "take": True, "crash": True, "correction": None}},
    {"case": "H10", "title": "B: negation (\"Anyone but Victor\")",
     "how": "Play sends the message; Resolve proposes and the customer confirms or corrects.",
     "plan": {"outage": False, "take": False, "crash": False,
              "correction": "No, not Victor. Anyone else, Thursday or Friday."}},
    {"case": "H15", "title": "C: reference (\"same time of day as my current booking\")",
     "how": "Play sends the message; if Resolve proposes the wrong time, the customer corrects it, then confirms "
            "during a database outage.",
     "plan": {"outage": False, "take": False, "crash": False, "outage_on_confirm": True,
              "correction": "No, keep 10:30 like my current booking. Any technician is fine."}},
]
live: dict = {}


@asynccontextmanager
async def lifespan(app):
    cases = {}
    for split in SPLITS:
        for c in C.load(C.CASE_DIR / f"{split}.yaml"):
            cases[c.id] = (split, c)
    live["cases"] = cases
    async with A.lifespan(app):  # the Resolve screen's reads (request detail, calendar, SQL verdict)
        session = Live(cases["D1"][1], os.environ.get("RESOLVE_LIVE_MODEL", "qwen3-14b-16k:latest"),
                       int(os.environ.get("RESOLVE_LIVE_MAX_CALLS", "40")),
                       float(os.environ.get("RESOLVE_LIVE_MAX_COST", "0.50")), echo=False)
        async with session:
            await session.tox.reset()
            live["s"] = session
            pump = asyncio.create_task(session.pump())
            try:
                yield
            finally:
                pump.cancel()


app = FastAPI(title="Resolve live flow", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def s() -> Live:
    return live["s"]


@app.get("/")
async def page():
    return FileResponse(PAGE)


@app.get("/api/live/cases")
async def cases():
    return {"presets": PRESETS,
            "all": [{"case": cid, "split": split, "kind": c.kind, "customer": c.raw["customer"],
                     "message": c.messages[0]} for cid, (split, c) in live["cases"].items()]}


class Start(BaseModel):
    case: str
    lane: str = Field(pattern="^(naive|resolve)$")


class Say(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class Switch(BaseModel):
    on: bool


@app.post("/api/live/start")
async def start(body: Start):
    x = s()
    if body.case not in live["cases"]:
        raise HTTPException(404, "no such case")
    if x.outage:
        raise HTTPException(409, "turn the outage off first: the world is re-seeded through the database")
    if x.busy:
        raise HTTPException(409, "the model is still reading the last message")
    x.use_case(live["cases"][body.case][1])
    await x.start_lane(body.lane)
    return {"request_id": x.rid}


@app.post("/api/live/say")
async def say(body: Say):
    x = s()
    if x.rid is None:
        raise HTTPException(409, "start a lane first")
    if x.busy:
        raise HTTPException(409, "the model is still reading the last message")
    x.busy = True  # set now, so a second click cannot start a second interpretation

    async def run():
        try:
            await x.customer(body.text)
        except Exception as exc:  # show it on the page; the session stays usable
            x.out("!", f"{type(exc).__name__}: {str(exc)[:200]}", "\033[33m")
            x.busy = False

    asyncio.create_task(run())
    return {"accepted": True}


@app.post("/api/live/outage")
async def outage(body: Switch):
    x = s()
    if body.on and not x.outage:
        await x.tox.add_latency("pg", 5000, 0, name=OUTAGE)
        x.outage = True
        x.out("OUTAGE", "FieldDesk's database link now has 5 s latency (Toxiproxy): its API calls time out",
              "\033[33m")
    elif not body.on and x.outage:
        await x.tox.remove("pg", OUTAGE)
        x.outage = False
        x.out("recovered", "latency removed", "\033[32m")
    return {"outage": x.outage}


@app.post("/api/live/take")
async def take():
    await s().command("/take")
    return {"ok": True}


@app.post("/api/live/crash")
async def crash(body: Switch):
    x = s()
    if body.on:
        await x.command("/crash")
    elif x.crash_next:
        x.crash_next = False
        x.out("crash", "disarmed", "\033[2m")
    return {"armed": x.crash_next}


async def world(x: Live) -> dict:
    """The case's technicians and every slot from the fixture day on, with who holds it now (FieldDesk's DB), and
    which technician's CalDAV calendar holds the customer's appointment."""
    case = x.case
    ids = [t["id"] for t in case.techs.values()]
    async with A.state["oracle"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT s.id, s.technician_id, s.starts_at, a.id AS appointment_id, a.customer_id FROM slots s "
            "LEFT JOIN appointments a ON a.slot_id = s.id AND a.status = 'scheduled' "
            "WHERE s.technician_id = ANY($1::bigint[]) AND s.slot_date >= $2 ORDER BY s.starts_at", ids, C.TODAY)
    original = 9_000_000 + case.n * 1000 + 901  # the current appointment's slot, as seeded
    slots = []
    for r in rows:
        held = ("ours" if r["appointment_id"] == case.appointment_id else
                "other" if r["appointment_id"] is not None else "open")
        slots.append({"slot_id": r["id"], "technician_id": r["technician_id"],
                      "starts_at": r["starts_at"].isoformat(timespec="minutes"), "held": held,
                      "original": r["id"] == original, "alias": case.alias_of(r["id"])})
    calendar: dict = {"configured": x.calendar is not None, "held_by": []}
    if x.calendar is not None:
        try:
            for tid in ids:
                body = await x.calendar.caldav.get_event(tid, case.appointment_id) or ""
                start = next((ln[8:] for ln in body.splitlines() if ln.startswith("DTSTART:")), None)
                if start:
                    calendar["held_by"].append({"technician_id": tid, "starts_at": dt.datetime.strptime(
                        start, "%Y%m%dT%H%M%S").isoformat(timespec="minutes")})
        except Exception as exc:  # the page shows the calendar as unreachable
            calendar["unreachable"] = type(exc).__name__
    auth = [[slot_label(case, k) for k in stage] for stage in case.expected["authorized"]]
    return {"case": case.id, "split": live["cases"][case.id][0], "kind": case.kind, "customer": case.raw["customer"],
            "title": case.raw.get("title"), "appointment_id": case.appointment_id,
            "technicians": [{"technician_id": t["id"], "name": t["name"]} for t in case.techs.values()],
            "slots": slots, "calendar": calendar, "today": C.TODAY.isoformat(),
            "script": {"messages": case.messages, "replies": case.replies},
            "taken": [slot_label(case, k) for k in case.taken],
            "graded": {"authorized": auth, "final": [slot_label(case, k) for k in case.expected["final"]],
                       "authorized_ids": [[case.slot_id(k) for k in stage] for stage in case.expected["authorized"]]},
            "plan": next((p["plan"] for p in PRESETS if p["case"] == case.id),
                         {"outage": False, "take": False, "crash": False, "correction": None})}


@app.get("/api/live/state")
async def state():
    x = s()
    req = await A.request(x.rid) if x.rid else None
    if req is not None and req.get("error"):
        req = None
    return {"session": {"lane": x.lane, "request_id": x.rid, "outage": x.outage, "busy": x.busy,
                        "crash_armed": x.crash_next, "model": x.model_name, "model_calls": len(x.model.log.calls),
                        "model_cost_usd": round(x.model.log.cost, 4)},
            "feed": x.feed[-150:], "world": await world(x), "request": req}
