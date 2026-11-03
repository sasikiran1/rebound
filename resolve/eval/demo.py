"""The Resolve demonstration, end to end on the live stack (synthetic fixture D1, scripted customer turns).

    uv run python -m resolve.eval.demo [--model qwen3-14b-16k:latest] [--case D1] [--outage-s 12]

 1. The customer asks for a conditional change (first message of the case).
 2. The model interprets it; the validated intent is stored as version 1.
 3. Outage: a 5 s latency toxic on FieldDesk's database link (Toxiproxy); the worker's search times out and the
    request waits in `ready`. While it waits, another customer books the first choice.
 4. The outage ends. The worker finds no authorized slot and asks a grounded question (`needs_input`).
 5. The customer's scripted reply corrects the request; the patched intent is stored as version 2 (diff shown).
    Product mode (default): the worker then asks "shall I book <slot>?" and the scripted customer says yes, so
    nothing is booked that the customer did not see; `--no-confirm` skips this, as in the frozen evaluation.
 6. A worker process is killed right after its booking POST returns (before it records the answer).
 7. A second worker process reclaims the expired lease, reconciles with the same idempotency key and confirms the
    booking without a second write.
 8. The independent SQL oracle judges the final database effect against the case file.
 9. With RESOLVE_CALDAV_URL set: the technicians' CalDAV calendars, read back before and after the change.
Every step is printed and the full record is written to runs/resolve/demo/<run>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path

import asyncpg
import httpx

from harness import settings
from harness.fault import Toxiproxy
from resolve import intent as I
from resolve.api import FieldDesk
from resolve.calendar import CalendarError, CalendarSync
from resolve.eval import cases as C
from resolve.eval.oracle import verdict
from resolve.models import make_model
from resolve.service import Resolve
from resolve.store import Store

T0 = time.monotonic()
CONFIRM_REPLY = "Yes, please book it."


def say(step: str, text: str) -> None:
    print(f"[{time.monotonic() - T0:6.1f}s] {step:<10} {text}", flush=True)


async def calendar_line(calendar: CalendarSync, case, names: dict) -> str:
    """Which technician's CalDAV calendar holds the appointment, read back from the calendar server."""
    held = []
    for tid, name in names.items():
        body = await calendar.caldav.get_event(tid, case.appointment_id) or ""
        start = next((x[8:] for x in body.splitlines() if x.startswith("DTSTART:")), None)
        if start:
            held.append(f"{name}'s calendar {dt.datetime.strptime(start, '%Y%m%dT%H%M%S'):%a %b %d %H:%M}")
    return "; ".join(held) or "in no technician's calendar"


async def worker_process(rid: str, crash_at: str | None = None, max_s: float = 60) -> list[dict]:
    cmd = [sys.executable, "-m", "resolve.worker", "--request", rid, "--max-s", str(max_s)]
    if crash_at:
        cmd += ["--crash-at", crash_at]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    lines = [json.loads(x) for x in out.decode().splitlines() if x.strip()]
    say("worker", f"pid {proc.pid} exited with code {proc.returncode}"
        + (" (killed itself right after the POST returned: answer never recorded)" if proc.returncode == 86 else ""))
    for x in lines:
        say("", json.dumps({k: v for k, v in x.items() if k in ("result", "reason", "slot_id", "change_id",
                                                               "replayed", "how", "state", "kind")}))
    return lines


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3-14b-16k:latest")
    ap.add_argument("--case", default="D1")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--outage-s", type=float, default=12.0)
    ap.add_argument("--run", default=None)
    ap.add_argument("--max-calls", type=int, default=6)
    ap.add_argument("--max-cost", type=float, default=0.25)
    ap.add_argument("--no-confirm", dest="confirm", action="store_false",
                    help="book authorized slots without the final yes (the frozen evaluation's setting)")
    a = ap.parse_args()
    run = a.run or f"demo-{dt.datetime.now():%Y%m%d-%H%M%S}"
    out = Path("runs/resolve/demo")
    out.mkdir(parents=True, exist_ok=True)
    case = next(c for c in C.load(C.CASE_DIR / f"{a.split}.yaml") if c.id == a.case)
    rid = f"{run}:{case.id}:resolve"
    admin = await asyncpg.connect(settings.admin_dsn(settings.TRIAL_DB))
    oracle = await asyncpg.connect(settings.oracle_dsn())
    store = await Store(settings.admin_dsn(settings.TRIAL_DB)).open()
    api = FieldDesk(settings.FIELDDESK_URL, actor="resolve-demo")
    model = make_model(a.model, out / run / "raw", a.max_calls, a.max_cost)
    svc = Resolve(store, api, model, C.TODAY, C.HORIZON_DAYS)
    names = {t["id"]: t["name"] for t in case.techs.values()}
    calendar = CalendarSync.from_env(store, api)  # technicians' CalDAV calendars, when RESOLVE_CALDAV_URL is set
    tox_http = httpx.AsyncClient()
    tox = Toxiproxy(tox_http)
    times: list[dt.datetime] = []
    record: dict = {"run": run, "case": case.id, "request_id": rid, "model": a.model, "steps": []}

    async def now() -> dt.datetime:
        return await oracle.fetchval("SELECT clock_timestamp()")

    try:
        await tox.reset()
        await C.seed(admin, case)
        await store.reset(rid)
        (out / f"{run}.json").write_text(json.dumps({"request_id": rid, "case": case.id, "split": a.split,
                                                     "turn_times": []}))
        say("setup", f"fixture {case.id} seeded in region {case.region}; synthetic data, scripted customer")
        if calendar is not None:
            techs = [{"technician_id": t["id"], "name": t["name"]} for t in case.techs.values()]
            try:
                await calendar.mirror(case.appointment_id, techs)
                say("calendar", "before: " + await calendar_line(calendar, case, names))
            except (CalendarError, httpx.HTTPError) as exc:  # the demo goes on without the calendar
                say("calendar", f"unreachable ({type(exc).__name__}); continuing without it")
                await calendar.close()
                calendar = None

        say("customer", case.messages[0])
        times.append(await now())
        await svc.open(rid, case.customer_id, case.appointment_id, case.region, case.service, case.messages[0],
                       confirm=a.confirm)
        req = await store.get(rid)
        v1 = await store.intent(rid, req["version"]) if req["version"] else None
        say("model", f"{len(model.log.calls)} call(s); state={req['state']} version={req['version']}")
        if v1:
            say("intent v1", I.explain(v1, names))
        for t in (await store.turns(rid))[1:]:
            say("assistant", t["text"])
        if req["state"] != "ready":
            raise SystemExit(f"stopped: request is {req['state']} after the first message")

        say("outage", "5 s latency on FieldDesk's database link (Toxiproxy); API calls now exceed 3 s")
        await tox.add_latency("pg", 5000, 0, name="resolve_outage")
        first = await worker_process(rid, max_s=4)
        record["steps"].append({"step": "during_outage", "worker": first})
        if case.taken:
            await C.take(admin, case)
            say("elsewhere", f"another customer booked {', '.join(case.taken)} while the API was down")
        await asyncio.sleep(max(0.0, a.outage_s - 4))
        await tox.remove("pg", "resolve_outage")
        say("recovered", "latency removed")

        second = await worker_process(rid, max_s=30)
        record["steps"].append({"step": "after_recovery", "worker": second})
        req = await store.get(rid)
        if req["state"] == "needs_input":
            q = req["question"]
            say("assistant", q["text"])
            reply = case.replies[0] if case.replies else "I don't have anything to add to what I already said."
            say("customer", reply)
            times.append(await now())
            res = await svc.customer_says(rid, reply)
            req = await store.get(rid)
            say("model", f"{len(model.log.calls)} call(s) total; state={req['state']} version={req['version']}"
                + (f" diff={res.get('diff')}" if res.get("diff") else ""))
            if req["version"] >= 2:
                say(f"intent v{req['version']}", I.explain(await store.intent(rid, req["version"]), names))
        if req["state"] == "ready" and a.confirm:
            confirm = await worker_process(rid, max_s=30)
            record["steps"].append({"step": "confirm_question", "worker": confirm})
            req = await store.get(rid)
            if req["state"] == "needs_input" and req["question"]["kind"] == "confirm":
                say("assistant", req["question"]["text"])
                say("customer", CONFIRM_REPLY + "   (scripted)")
                times.append(await now())
                await svc.customer_says(rid, CONFIRM_REPLY)
                req = await store.get(rid)
                say("approved", f"version {req['version']}: the customer approved that exact slot")
        (out / f"{run}.json").write_text(json.dumps({"request_id": rid, "case": case.id, "split": a.split,
                                                     "turn_times": [t.isoformat() for t in times]}))
        if req["state"] == "ready":
            say("crash", "starting a worker that dies right after its booking POST returns")
            third = await worker_process(rid, crash_at="after_send", max_s=30)
            req = await store.get(rid)
            say("store", f"state={req['state']} attempt={req['attempt_state']} slot={req['attempt_slot']} "
                f"lease_until={req['lease_until'] and req['lease_until'].isoformat(timespec='seconds')}")
            changes = await oracle.fetchval("SELECT count(*) FROM appointment_changes WHERE appointment_id = $1",
                                            case.appointment_id)
            say("sql", f"appointment_changes rows for the appointment: {changes} (the commit happened)")
            say("restart", "a new worker process waits for the dead worker's lease to expire, then reconciles")
            fourth = await worker_process(rid, max_s=40)
            record["steps"].append({"step": "crash", "worker": third})
            record["steps"].append({"step": "restart", "worker": fourth})
        v = await verdict(oracle, case, times)
        record["verdict"] = v
        rows = await oracle.fetch("SELECT id, from_slot_id, to_slot_id, idempotency_key, applied_at FROM "
                                  "appointment_changes WHERE appointment_id = $1 ORDER BY id", case.appointment_id)
        say("sql", f"{len(rows)} change row(s): " + "; ".join(
            f"#{r['id']} -> {case.alias_of(r['to_slot_id'])} key={r['idempotency_key']}" for r in rows))
        say("oracle", f"verdict={v['verdict']} unauthorized={v['unauthorized']} final={v['final_slot']} "
            f"(expected one of {case.expected['final'] or 'no booking'})")
        for t in (await store.turns(rid))[-1:]:
            say("assistant", t["text"])
        if calendar is not None and (await store.get(rid))["state"] == "completed":
            synced = await calendar.drain(request_id=rid)
            record["calendar"] = {"drain": synced, "sync": await store.calendar_sync(rid)}
            say("calendar", "after: " + await calendar_line(calendar, case, names) + f"  (sync {synced})")
    finally:
        if calendar is not None:
            await calendar.close()
        try:
            await tox.reset()
        except httpx.HTTPError:
            pass
        await tox_http.aclose()
        record["turns"] = [{"role": t["role"], "kind": t["kind"], "text": t["text"]} for t in await store.turns(rid)]
        record["versions"] = [{"version": v["version"], "intent": v["intent"], "approved_by": v["approved_by"],
                               "diff": v["diff"]} for v in await store.versions(rid)]
        ev = await store.pool.fetch("SELECT kind, data, at FROM resolve.events WHERE request_id = $1 ORDER BY id",
                                    rid)
        record["events"] = [{"kind": e["kind"], "data": json.loads(e["data"]), "at": e["at"].isoformat()}
                            for e in ev]
        record["model_calls"] = model.log.calls
        record["model_cost_usd"] = round(model.log.cost, 6)
        record["turn_times"] = [t.isoformat() for t in times]
        (out / run).mkdir(parents=True, exist_ok=True)
        (out / run / "record.json").write_text(json.dumps(record, indent=1, default=str, ensure_ascii=False))
        (out / f"{run}.json").write_text(json.dumps({"request_id": rid, "case": case.id, "split": a.split,
                                                     "turn_times": record["turn_times"]}))
        await api.close()
        await store.close()
        await admin.close()
        await oracle.close()
        if hasattr(model.client, "close"):
            await model.client.close()
    say("done", f"record: {out / run / 'record.json'}")


if __name__ == "__main__":
    asyncio.run(main())
