"""Recovery and validation checks for the Resolve path, on the live stack (FieldDesk, PostgreSQL, Toxiproxy).

    uv run python -m resolve.eval.faults [--only truncation,crash_after_send,...]

Where the check is about the deterministic path, the model is replaced by canned outputs (CannedModel), so a pass or
fail cannot depend on model luck; the canned outputs are listed in each check. Every check judges the database with
the SQL oracle and writes runs/resolve/faults/<run>/report.json.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import httpx

from harness import settings
from harness.fault import Toxiproxy
from resolve.api import PAGE, FieldDesk
from resolve.eval import cases as C
from resolve.eval.oracle import verdict
from resolve.eval.run import form_intent
from resolve.interpret import CallLog
from resolve.service import Resolve
from resolve.store import Store
from resolve.worker import Worker

FAULTS = C.CASE_DIR / "faults.yaml"


@dataclass
class CannedModel:
    """Stands in for the model: returns the queued tool arguments in order and records each call."""
    outputs: list
    name: str = "canned"
    log: CallLog = field(default_factory=CallLog)

    async def tool_call(self, purpose, instructions, user, tool, extra=None):
        args = self.outputs.pop(0)
        self.log.calls.append({"purpose": purpose, "status": "ok", "cost_usd": 0.0, "latency_ms": 0,
                               "input_tokens": 0, "output_tokens": 0, "arguments": args})
        return args, ""


class Env:
    def __init__(self, run: str):
        self.run = run

    async def __aenter__(self):
        self.admin = await asyncpg.connect(settings.admin_dsn(settings.TRIAL_DB))
        self.oracle = await asyncpg.connect(settings.oracle_dsn())
        self.store = await Store(settings.admin_dsn(settings.TRIAL_DB)).open()
        self.api = FieldDesk(settings.FIELDDESK_URL, actor="resolve-faults")
        self.tox_http = httpx.AsyncClient()
        self.tox = Toxiproxy(self.tox_http)
        self.cases = {c.id: c for c in C.load(FAULTS)}
        return self

    async def __aexit__(self, *exc):
        await self.tox.reset()
        await self.tox_http.aclose()
        await self.api.close()
        await self.store.close()
        await self.admin.close()
        await self.oracle.close()

    async def now(self):
        return await self.oracle.fetchval("SELECT clock_timestamp()")

    async def fresh(self, case_id: str, check: str):
        case = self.cases[case_id]
        rid = f"{self.run}:{check}"
        await C.seed(self.admin, case)
        await self.store.reset(rid)
        return case, rid

    async def structured(self, case, rid: str, form: dict | None = None, confirm: bool = False) -> list:
        """Open a request with a structured (form) intent; returns the turn times."""
        facts = await Resolve(self.store, self.api, None, C.TODAY, C.HORIZON_DAYS).load_facts(
            case.customer_id, case.appointment_id)
        await self.store.create(rid, case.customer_id, case.appointment_id, case.region, case.service, C.TODAY,
                                C.HORIZON_DAYS, facts, confirm)
        times = [await self.now()]
        svc = Resolve(self.store, self.api, None, C.TODAY, C.HORIZON_DAYS)
        await svc.approve_structured(rid, form_intent(case, form or case.raw["form"]), "form", "structured")
        return times

    async def changes(self, case) -> list:
        return await self.oracle.fetch("SELECT id, to_slot_id, idempotency_key FROM appointment_changes "
                                       "WHERE appointment_id = $1 ORDER BY id", case.appointment_id)


async def worker_proc(rid: str, crash_at: str | None = None, max_s: float = 40) -> tuple[int, list[dict]]:
    cmd = [sys.executable, "-m", "resolve.worker", "--request", rid, "--max-s", str(max_s), "--lease-s", "4"]
    if crash_at:
        cmd += ["--crash-at", crash_at]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return proc.returncode, [json.loads(x) for x in out.decode().splitlines() if x.strip()]


async def events(env: Env, rid: str) -> list[dict]:
    rows = await env.store.pool.fetch("SELECT kind, data FROM resolve.events WHERE request_id = $1 ORDER BY id", rid)
    return [{"kind": r["kind"], **json.loads(r["data"])} for r in rows]


async def check_truncation(env: Env) -> dict:
    case, rid = await env.fresh("F1", "truncation")
    first = await env.api._call("search", "GET", "/availability/search", params={
        "service": case.service, "from": C.TODAY.isoformat(),
        "to": (C.TODAY + dt.timedelta(days=C.HORIZON_DAYS)).isoformat(), "regions": case.region})
    target = case.slot_id("fri2 16:00 dana")
    times = await env.structured(case, rid)
    res = await Worker(env.store, env.api, owner="w-trunc").run_once(rid)
    v = await verdict(env.oracle, case, times)
    on_first_page = any(s["slot_id"] == target for s in first["slots"])
    return {"passed": v["verdict"] == "correct" and not on_first_page,
            "first_page_rows": len(first["slots"]), "page_limit": PAGE, "total_count": first["total_count"],
            "eligible_on_first_page": on_first_page, "worker": res, "verdict": v}


async def check_crash(env: Env, point: str) -> dict:
    case, rid = await env.fresh("F2", f"crash_{point}")
    times = await env.structured(case, rid)
    code1, out1 = await worker_proc(rid, crash_at=point)
    mid = await env.store.get(rid)
    rows_mid = await env.changes(case)
    code2, out2 = await worker_proc(rid)
    v = await verdict(env.oracle, case, times)
    ev = await events(env, rid)
    reconciled = [e for e in ev if e["kind"] == "worker.reconciled"]
    expect_replay = point == "after_send"
    ok = (code1 == 86 and code2 == 0 and v["verdict"] == "correct" and v["changes"] == 1
          and (not expect_replay or (reconciled and reconciled[0].get("replayed") is True)))
    return {"passed": ok, "crash_exit": code1, "after_crash": {"state": mid["state"],
                                                               "attempt_state": mid["attempt_state"],
                                                               "changes": len(rows_mid)},
            "restart_exit": code2, "restart": out2, "reconciled": reconciled, "verdict": v}


async def check_lost_ack(env: Env) -> dict:
    """The POST reaches FieldDesk, the commit lands, but the answer arrives after the worker's 3 s deadline."""
    case, rid = await env.fresh("F2", "lost_ack")
    times = await env.structured(case, rid)
    applied = {}

    async def slow_link(req, slot):
        await env.tox.add_latency("pg", 900, 0, name="lost_ack")  # each DB round trip +0.9 s: the POST takes > 3 s
        applied["slot"] = slot["slot_id"]

    w = Worker(env.store, env.api, owner="w-lostack", lease_s=4, before_post=slow_link)
    first = await w.run_once(rid)
    mid = await env.store.get(rid)
    landed = None
    for _ in range(40):  # the server keeps going after the client gave up
        if await env.changes(case):
            landed = await env.now()
            break
        await asyncio.sleep(0.25)
    await env.tox.remove("pg", "lost_ack")
    w.before_post = None
    second = await w.run_once(rid)
    v = await verdict(env.oracle, case, times)
    ok = (first["result"] == "retry_later" and mid["attempt_state"] == "sending" and landed is not None
          and second["result"] == "completed" and second.get("replayed") is True and v["verdict"] == "correct"
          and v["changes"] == 1)
    return {"passed": ok, "first_pass": first, "after_timeout": {"state": mid["state"],
                                                                 "attempt_state": mid["attempt_state"]},
            "commit_landed_after_timeout": landed is not None, "second_pass": second, "verdict": v}


async def check_renewed_permission(env: Env) -> dict:
    case, rid = await env.fresh("F3", "renewed_permission")
    a = case.techs["a"]["id"]
    thu10 = case.slot_id("thu 10:00 a")
    model = CannedModel([
        {"rules": [{"kind": "on_dates", "dates": ["2026-11-11"], "strength": "flexible", "quote": "Wednesday"},
                   {"kind": "technician_is", "technician_ids": [a], "strength": "flexible", "quote": "with Ana"}],
         "summary": "Wednesday with Ana"},
        {"ops": [{"op": "accept_proposal", "slot_id": thu10}], "summary": "Thursday at 10 with Ana"},
    ])
    svc = Resolve(env.store, env.api, model, C.TODAY, C.HORIZON_DAYS)
    times = [await env.now()]
    await svc.open(rid, case.customer_id, case.appointment_id, case.region, case.service, case.messages[0])
    w = Worker(env.store, env.api, owner="w-perm")
    first = await w.run_once(rid)
    times.append(await env.now())
    await svc.customer_says(rid, case.replies[0])
    await C.take(env.admin, case, ["thu 10:00 a"])  # the approved slot disappears before execution
    second = await w.run_once(rid)
    req = await env.store.get(rid)
    v = await verdict(env.oracle, case, times)
    ok = (first["result"] == "needs_input" and thu10 in first["candidates"] and second["result"] == "needs_input"
          and v["changes"] == 0 and req["questions_asked"] == 2)
    return {"passed": ok, "first_question": first, "after_taken": second, "verdict": v,
            "canned_model_outputs": [c["arguments"] for c in model.log.calls]}


async def check_correction(env: Env) -> dict:
    case, rid = await env.fresh("F4", "correction")
    after2 = {"kind": "start_at_or_after", "time": "14:00", "strength": "flexible", "quote": "after 2pm"}
    model = CannedModel([
        {"rules": [{"kind": "on_dates", "dates": ["2026-11-13"], "strength": "flexible", "quote": "Friday"}],
         "summary": "Friday"},
        {"ops": [{"op": "replace_rule", "rule_id": "r1", "rule": after2}], "summary": "after 2pm"},  # drops Friday
        {"ops": [{"op": "add_rule", "rule": after2}], "summary": "Friday after 2pm"},
    ])
    svc = Resolve(env.store, env.api, model, C.TODAY, C.HORIZON_DAYS)
    times = [await env.now()]
    await svc.open(rid, case.customer_id, case.appointment_id, case.region, case.service, case.messages[0])
    times.append(await env.now())
    res = await svc.customer_says(rid, case.messages[1])
    versions = await env.store.versions(rid)
    await Worker(env.store, env.api, owner="w-corr").run_once(rid)
    v = await verdict(env.oracle, case, times)
    v2 = versions[-1]["intent"]["rules"] if len(versions) > 1 else []
    ev = [e for e in await events(env, rid) if e["kind"] == "revise"]
    ok = (len(versions) == 2 and any(r["kind"] == "on_dates" for r in v2) and v["verdict"] == "correct"
          and len(model.log.calls) == 3)
    return {"passed": ok, "revise": res, "version2_rules": v2, "revise_event": ev, "verdict": v,
            "note": "the first canned patch replaces the Friday rule with a time rule; it must be rejected"}


async def check_confirm(env: Env) -> dict:
    """Product mode: an authorized slot is still not booked until the customer says yes to that exact slot."""
    case, rid = await env.fresh("F2", "confirm_mode")
    times = await env.structured(case, rid, confirm=True)
    w = Worker(env.store, env.api, owner="w-confirm")
    first = await w.run_once(rid)
    before = len(await env.changes(case))
    times.append(await env.now())
    svc = Resolve(env.store, env.api, None, C.TODAY, C.HORIZON_DAYS)  # no model: a plain yes is matched exactly
    await svc.customer_says(rid, "Yes, please book it.")
    second = await w.run_once(rid)
    v = await verdict(env.oracle, case, times)
    ok = (first["result"] == "needs_input" and first["kind"] == "confirm" and before == 0
          and second["result"] == "completed" and v["verdict"] == "correct")
    return {"passed": ok, "first_pass": first, "changes_before_yes": before, "second_pass": second, "verdict": v}


async def check_notifications(env: Env) -> dict:
    """The confirmation question and the final outcome reach the customer by email; while the mail server refuses,
    the message stays queued and is delivered on a later pass."""
    from resolve.eval.mailsink import MailSink
    from resolve.notify import Notifier
    case, rid = await env.fresh("F2", "notifications")
    sink = await MailSink().start("127.0.0.1", 2525)
    try:
        notifier = Notifier(env.store, "127.0.0.1", 2525)
        times = await env.structured(case, rid, confirm=True)
        w = Worker(env.store, env.api, owner="w-notify")
        await w.run_once(rid)  # asks "shall I book it?": a notification is queued with the question
        sink.refuse = True
        refused = await notifier.drain(request_id=rid)
        queued = (await env.store.notifications(rid))[0]
        sink.refuse = False
        await asyncio.sleep(5.5)  # first retry is due 5 s after a failure
        retried = await notifier.drain(request_id=rid)
        times.append(await env.now())
        await Resolve(env.store, env.api, None, C.TODAY, C.HORIZON_DAYS).customer_says(rid, "Yes, please book it.")
        await w.run_once(rid)
        final = await notifier.drain(request_id=rid)
        rows = await env.store.notifications(rid)
        v = await verdict(env.oracle, case, times)
    finally:
        await sink.stop()
    # The notifier drains this request only (other runs may leave due messages; several checks share this fixture's
    # customer); judge this request's own, by its Message-ID.
    mine = [m for m in sink.messages if m["message_id"].startswith(f"<{rid.replace(':', '.')}.")]
    subjects = [m["subject"] for m in mine]
    ok = (queued["status"] == "pending" and queued["attempts"] == 1 and queued["last_error"]
          and len(mine) == len(rows) == 2 and all(r["status"] == "sent" for r in rows)
          and any("confirm" in s for s in subjects) and any("moved" in s for s in subjects)
          and v["verdict"] == "correct")
    return {"passed": ok, "refused_pass": refused, "queued_after_refusal": {k: queued[k] for k in (
        "status", "attempts", "last_error")}, "retry_pass": retried, "final_pass": final,
            "received": [{k: m[k] for k in ("to", "subject")} for m in mine], "verdict": v}


async def check_calendar(env: Env) -> dict:
    """The booking appears in the assigned technician's CalDAV calendar (a real Radicale server). While the calendar
    server is down the sync stays queued, the booking does not wait, and a later pass catches up; copies in the other
    technicians' calendars are removed; a second pass changes nothing."""
    import shutil
    import tempfile

    from resolve.calendar import CalDAV, CalendarSync, event_ics
    from resolve.eval.calserver import USER, CalServer
    case, rid = await env.fresh("F2", "calendar")
    folder = Path(tempfile.mkdtemp(prefix="rebound-cal-"))
    server = await CalServer(5298, folder).start()
    caldav = CalDAV(server.url, USER, server.password)
    sync = CalendarSync(env.store, env.api, caldav)
    try:
        times = await env.structured(case, rid)
        techs = (await env.store.get(rid))["facts"]["technicians"]
        ids = [t["technician_id"] for t in techs]
        before = await env.api.appointment(case.appointment_id)
        await sync.mirror(case.appointment_id, techs)  # the calendar shows the original time
        for t in ids:  # stale copies elsewhere, as if an earlier sync had left them
            if t != before["technician_id"]:
                await caldav.ensure_calendar(t, str(t))
                await caldav.put_event(t, case.appointment_id, event_ics(before, str(t), "stale", None))
        await server.stop()
        res = await Worker(env.store, env.api, owner="w-calendar").run_once(rid)  # books while the calendar is down
        down = await sync.drain(request_id=rid)
        queued = await env.store.calendar_sync(rid)
        await server.start()
        await asyncio.sleep(5.5)  # first retry is due 5 s after a failure
        up = await sync.drain(request_id=rid)
        again = await sync.drain(request_id=rid)
        row = await env.store.calendar_sync(rid)
        after = await env.api.appointment(case.appointment_id)
        holders = {t: await caldav.events(t) for t in ids}
        body = await caldav.get_event(after["technician_id"], case.appointment_id) or ""
        times.append(await env.now())
        v = await verdict(env.oracle, case, times)
    finally:
        await caldav.close()
        await server.stop()
        shutil.rmtree(folder, ignore_errors=True)
    name = f"appointment-{case.appointment_id}.ics"
    start = dt.datetime.fromisoformat(after["starts_at"])
    ok = (res["result"] == "completed" and down == {"synced": 0, "retry": 1, "failed": 0}
          and queued["status"] == "pending" and queued["attempts"] == 1 and bool(queued["last_error"])
          and up["synced"] == 1 and again == {"synced": 0, "retry": 0, "failed": 0} and row["status"] == "synced"
          and all((name in files) == (t == after["technician_id"]) for t, files in holders.items())
          and f"DTSTART:{start:%Y%m%dT%H%M%S}" in body and after["slot_id"] != before["slot_id"]
          and v["verdict"] == "correct")
    return {"passed": ok, "before": {k: before[k] for k in ("slot_id", "starts_at", "technician_id")},
            "after": {k: after[k] for k in ("slot_id", "starts_at", "technician_id")},
            "moved_technician": after["technician_id"] != before["technician_id"], "worker": res["result"],
            "calendar_down_pass": down, "queued_while_down": {k: queued[k] for k in ("status", "attempts",
                                                                                    "last_error")},
            "calendar_up_pass": up, "second_pass": again, "event_url": row["event_url"],
            "calendars": holders, "verdict": v}


async def _toxic(env: Env, kind: str, attributes: dict, name: str) -> None:
    r = await env.tox.client.post(f"{env.tox.base}/proxies/pg/toxics", json={
        "name": name, "type": kind, "stream": "downstream", "toxicity": 1.0, "attributes": attributes})
    r.raise_for_status()


async def _passes_until_done(w: Worker, env: Env, rid: str, n: int = 8) -> list[dict]:
    out = []
    for _ in range(n):
        req = await env.store.get(rid)
        if req["state"] != "ready" and not (req["state"] == "claimed"):
            break
        res = await w.run_once(rid)
        out.append(res)
        if res is None or res.get("result") == "retry_later":
            await asyncio.sleep(1.5)
    return out


async def check_fault_shapes(env: Env) -> dict:
    """Beyond latency: the database link is reset mid-write, or goes silent mid-search (Toxiproxy reset_peer and
    timeout toxics). Either way the worker must end with exactly one correct change once the link is back."""
    out = {}
    for shape, kind, during in (("reset_during_write", "reset_peer", "write"),
                                ("blackhole_during_search", "timeout", "search")):
        case, rid = await env.fresh("F2", shape)
        times = await env.structured(case, rid)
        name = f"shape_{shape}"

        async def on_write(req, slot, kind=kind, name=name):
            await _toxic(env, kind, {"timeout": 0}, name)

        w = Worker(env.store, env.api, owner=f"w-{shape}", lease_s=4,
                   before_post=on_write if during == "write" else None)
        if during == "search":
            await _toxic(env, kind, {"timeout": 0}, name)
        first = await w.run_once(rid)
        mid = await env.store.get(rid)
        await env.tox.remove("pg", name)
        w.before_post = None
        rest = await _passes_until_done(w, env, rid)
        v = await verdict(env.oracle, case, times)
        out[shape] = {"passed": first["result"] == "retry_later" and v["verdict"] == "correct" and v["changes"] == 1,
                      "first_pass": first, "after_fault": {"state": mid["state"], "attempt": mid["attempt_state"]},
                      "later_passes": rest, "verdict": v}
    return {"passed": all(x["passed"] for x in out.values()), "shapes": out}


INVALID = {
    "reversed_range": {"kind": "dates_between", "date_from": "2026-11-13", "date_to": "2026-11-11"},
    "not_a_date": {"kind": "on_dates", "dates": ["2026-11-31"]},
    "outside_horizon": {"kind": "on_dates", "dates": ["2026-12-15"]},
    "invented_technician": {"kind": "technician_is", "technician_ids": [123456]},
    "unnamed_technician": "unnamed",
    "quote_not_said": "quote",
    "impossible_window": "window",
}


async def check_invalid(env: Env) -> dict:
    out = {}
    for name, bad in INVALID.items():
        case, rid = await env.fresh("F2", f"invalid_{name}")
        base = {"kind": "on_dates", "dates": ["2026-11-12"], "strength": "flexible", "quote": "Thursday morning"}
        if bad == "unnamed":
            rules = [base, {"kind": "technician_is", "technician_ids": [case.techs["b"]["id"]],
                            "strength": "flexible", "quote": "Thursday morning"}]
        elif bad == "quote":
            rules = [{**base, "quote": "any Thursday you like"}]
        elif bad == "window":
            rules = [base, {"kind": "start_at_or_after", "time": "16:00", "strength": "flexible",
                            "quote": "Thursday morning"},
                     {"kind": "start_before", "time": "09:00", "strength": "flexible", "quote": "Thursday morning"}]
        else:
            rules = [{**bad, "strength": "flexible", "quote": "Thursday morning"}]
        intent = {"rules": rules, "summary": "canned invalid intent"}
        model = CannedModel([intent, intent])  # the same invalid output on the repair call too
        svc = Resolve(env.store, env.api, model, C.TODAY, C.HORIZON_DAYS)
        times = [await env.now()]
        await svc.open(rid, case.customer_id, case.appointment_id, case.region, case.service,
                       "Thursday morning please.")
        req = await env.store.get(rid)
        if req["state"] == "ready":
            await Worker(env.store, env.api, owner="w-invalid").run_once(rid)
        v = await verdict(env.oracle, case, times)
        ev = [e for e in await events(env, rid) if e["kind"] == "interpret"]
        out[name] = {"passed": req["state"] in ("unresolved", "needs_input") and v["changes"] == 0,
                     "state": req["state"], "problems": ev[0]["problems"] if ev else None, "changes": v["changes"]}
    return {"passed": all(x["passed"] for x in out.values()), "cases": out}


async def _uncertain_v1(env: Env, case, rid: str, slot_key: str) -> dict:
    """A worker recorded a v1 attempt (`sending`), then gave up without an answer and released the request: the state
    a timeout leaves behind (the request is `ready` again with the attempt kept)."""
    req = await env.store.claim("w-old", 8, rid, honor_backoff=False)
    key = f"resolve:{rid}:v{req['version']}"
    await env.store.record_attempt(rid, "w-old", case.slot_id(slot_key), key, "sending", gen=req["lease_gen"])
    await env.store.set_state(rid, "ready", owner="w-old", gen=req["lease_gen"])
    return {"key": key, "slot_id": case.slot_id(slot_key)}


async def check_superseded_attempt(env: Env) -> dict:
    """The customer corrects the request while an unconfirmed v1 write is outstanding (it never reached FieldDesk).
    The worker must not resend v1: it voids v1's key and books by v2; a late copy of the v1 write is then refused."""
    case, rid = await env.fresh("F4", "superseded_attempt")
    times = await env.structured(case, rid)  # v1: Friday, any time
    old = await _uncertain_v1(env, case, rid, "fri 09:00 gus")
    times.append(await env.now())
    svc = Resolve(env.store, env.api, None, C.TODAY, C.HORIZON_DAYS)
    v2 = await svc.approve_structured(rid, form_intent(case, {"rules": [
        {"kind": "on_dates", "dates": ["2026-11-13"]}, {"kind": "start_at_or_after", "time": "14:00"}]}),
        "form", "structured correction: after 2pm")
    res = await Worker(env.store, env.api, owner="w-new").run_once(rid)
    late = None
    try:  # the old write arrives late, with its own key
        await env.api.reschedule(case.appointment_id, case.customer_id, old["slot_id"], old["key"])
        late = "applied"
    except Exception as exc:  # noqa: BLE001 - the expected refusal is an ApiError
        late = getattr(exc, "body", {}).get("type") or type(exc).__name__
    v = await verdict(env.oracle, case, times)
    ev = [e for e in await events(env, rid) if e["kind"] == "worker.superseded_attempt"]
    ok = (v2 == 2 and res["result"] == "completed" and ev and ev[0]["outcome"] == "voided" and late == "key_voided"
          and v["verdict"] == "correct" and v["changes"] == 1)
    return {"passed": ok, "worker": res, "superseded": ev, "late_v1_write": late, "verdict": v,
            "before_fix": "the worker resent v1 (Fri 09:00 Gus) and completed: unauthorized after the correction"}


async def check_superseded_committed(env: Env) -> dict:
    """Same, but the v1 write did commit before the correction (its answer was lost). The worker learns that from
    the key without resending it, reports the change, then moves the appointment to what v2 allows."""
    case, rid = await env.fresh("F4", "superseded_committed")
    times = await env.structured(case, rid)
    old = await _uncertain_v1(env, case, rid, "fri 09:00 gus")
    await env.api.reschedule(case.appointment_id, case.customer_id, old["slot_id"], old["key"])  # landed, ack lost
    times.append(await env.now())
    svc = Resolve(env.store, env.api, None, C.TODAY, C.HORIZON_DAYS)
    await svc.approve_structured(rid, form_intent(case, {"rules": [
        {"kind": "on_dates", "dates": ["2026-11-13"]}, {"kind": "start_at_or_after", "time": "14:00"}]}),
        "form", "structured correction: after 2pm")
    res = await Worker(env.store, env.api, owner="w-new").run_once(rid)
    rows = await env.changes(case)
    ev = [e for e in await events(env, rid) if e["kind"] == "worker.superseded_attempt"]
    final = await env.oracle.fetchval("SELECT slot_id FROM appointments WHERE id = $1", case.appointment_id)
    ok = (ev and ev[0]["outcome"] == "applied" and res["result"] == "completed" and len(rows) == 2
          and rows[0]["idempotency_key"] == old["key"] and final == case.slot_id("fri 14:30 ida"))
    return {"passed": ok, "worker": res, "superseded": ev, "changes": [dict(r) for r in rows],
            "note": "two changes, each authorized when made: v1 committed before the correction, v2 after it"}


FRIDAY = {"rules": [{"kind": "on_dates", "dates": ["2026-11-13"], "strength": "flexible", "quote": "Friday"}],
          "summary": "Friday"}
AFTER2 = {"ops": [{"op": "add_rule", "rule": {"kind": "start_at_or_after", "time": "14:00", "strength": "flexible",
                                              "quote": "after 2pm"}}], "summary": "Friday after 2pm"}
DECLINE = {"ops": [{"op": "decline"}], "summary": "leave it"}


async def _open_with_uncertain_v1(env: Env, check: str, outputs: list):
    """F4 opened by a (canned) message, then an unconfirmed v1 write to Fri 09:00 Gus left behind by a timeout."""
    case, rid = await env.fresh("F4", check)
    model = CannedModel([FRIDAY] + outputs)
    svc = Resolve(env.store, env.api, model, C.TODAY, C.HORIZON_DAYS)
    await svc.open(rid, case.customer_id, case.appointment_id, case.region, case.service, case.messages[0])
    old = await _uncertain_v1(env, case, rid, "fri 09:00 gus")
    return case, rid, model, old


async def _late_v1(env: Env, case, old) -> str:
    try:
        await env.api.reschedule(case.appointment_id, case.customer_id, old["slot_id"], old["key"])
        return "applied"
    except Exception as exc:  # noqa: BLE001 - the expected refusal is an ApiError
        return getattr(exc, "body", {}).get("type") or type(exc).__name__


def _unreachable(env: Env) -> FieldDesk:
    return FieldDesk("http://127.0.0.1:9", timeout_s=1.0, actor="resolve-faults-unreachable")  # nothing listens


async def check_correction_settles_first(env: Env) -> dict:
    """A correction arrives while a v1 write is in doubt and FieldDesk is reachable: the conversation voids the v1
    key before it accepts the correction, so v2 takes effect at once and a late v1 write is refused."""
    case, rid, model, old = await _open_with_uncertain_v1(env, "correction_settles_first", [AFTER2])
    svc = Resolve(env.store, env.api, model, C.TODAY, C.HORIZON_DAYS)
    res = await svc.customer_says(rid, case.messages[1])
    after = await env.store.get(rid)
    late = await _late_v1(env, case, old)
    w = await Worker(env.store, env.api, owner="w-settle").run_once(rid)
    rows = await env.changes(case)
    final = await env.oracle.fetchval("SELECT slot_id FROM appointments WHERE id = $1", case.appointment_id)
    ev = [e for e in await events(env, rid) if e["kind"] == "service.settled_before_turn"]
    ok = (res.get("earlier_write") == "voided" and not res.get("pending") and after["version"] == 2
          and after["pending_version"] is None and late == "key_voided" and w["result"] == "completed"
          and len(rows) == 1 and final == case.slot_id("fri 14:30 ida"))
    return {"passed": ok, "correction": res, "late_v1_write": late, "worker": w, "settled": ev,
            "changes": [dict(r) for r in rows]}


async def check_correction_waits_for_settlement(env: Env) -> dict:
    """The ordering the review named: the correction arrives while FieldDesk cannot be reached, then the old v1 write
    commits. The correction must not have taken effect before that: it stays pending (version 1 current), the worker
    settles the v1 key (applied), and only then makes v2 current and moves the appointment; the customer is told."""
    case, rid, model, old = await _open_with_uncertain_v1(env, "correction_waits", [AFTER2])
    down = _unreachable(env)
    try:
        res = await Resolve(env.store, down, model, C.TODAY, C.HORIZON_DAYS).customer_says(rid, case.messages[1])
    finally:
        await down.close()
    pending = await env.store.get(rid)
    late = await _late_v1(env, case, old)  # the old write lands after the correction was received
    w = await Worker(env.store, env.api, owner="w-pending").run_once(rid)
    rows = await env.oracle.fetch("SELECT id, to_slot_id, idempotency_key, applied_at FROM appointment_changes "
                                  "WHERE appointment_id = $1 ORDER BY id", case.appointment_id)
    ev = await env.store.pool.fetch("SELECT kind, data, at FROM resolve.events WHERE request_id = $1 AND kind IN "
                                    "('worker.superseded_attempt', 'worker.correction_applied') ORDER BY id", rid)
    applied_at = rows[0]["applied_at"] if rows else None
    promoted_at = next((e["at"] for e in ev if e["kind"] == "worker.correction_applied"), None)
    told = [t["text"] for t in await env.store.turns(rid) if "went through before I could stop it" in t["text"]]
    final = await env.oracle.fetchval("SELECT slot_id FROM appointments WHERE id = $1", case.appointment_id)
    ok = bool(res.get("pending") and pending["version"] == 1 and pending["pending_version"] == 2
              and late == "applied" and w["result"] == "completed" and promoted_at is not None
              and applied_at is not None and applied_at < promoted_at and len(rows) == 2
              and final == case.slot_id("fri 14:30 ida") and told)
    return {"passed": ok, "correction": res, "after_correction": {"version": pending["version"],
                                                                    "pending_version": pending["pending_version"]},
            "late_v1_write": late, "worker": w, "v1_applied_at": applied_at, "v2_took_effect_at": promoted_at,
            "customer_told": told,
            "note": "the v1 write was sent before the correction; it commits before v2 takes effect, never after"}


async def check_decline_settles(env: Env) -> dict:
    """Declining while a write is in doubt: reachable, the key is voided and nothing changes; unreachable, the close
    waits for the worker, which settles the write (here it had gone through) and tells the customer so."""
    out = {}
    case, rid, model, old = await _open_with_uncertain_v1(env, "decline_reachable", [DECLINE])
    res = await Resolve(env.store, env.api, model, C.TODAY, C.HORIZON_DAYS).customer_says(rid, "No, leave it.")
    late = await _late_v1(env, case, old)
    req = await env.store.get(rid)
    out["reachable"] = {"passed": req["state"] == "unresolved" and late == "key_voided" and not await env.changes(case),
                        "reply": res, "late_v1_write": late}
    case, rid, model, old = await _open_with_uncertain_v1(env, "decline_unreachable", [DECLINE])
    down = _unreachable(env)
    try:
        res = await Resolve(env.store, down, model, C.TODAY, C.HORIZON_DAYS).customer_says(rid, "No, leave it.")
    finally:
        await down.close()
    waiting = await env.store.get(rid)
    late = await _late_v1(env, case, old)
    w = await Worker(env.store, env.api, owner="w-decline").run_once(rid)
    req = await env.store.get(rid)
    last = (await env.store.turns(rid))[-1]["text"]
    out["unreachable"] = {"passed": bool(res.get("pending_close")) and waiting["state"] == "ready" and late == "applied"
                          and w["result"] == "unresolved" and w.get("settled") == "applied"
                          and req["state"] == "unresolved" and "went through" in last,
                          "reply": res, "late_v1_write": late, "worker": w, "last_message": last}
    return {"passed": all(x["passed"] for x in out.values()), **out}


async def check_stale_worker(env: Env) -> dict:
    """Worker A pauses past its lease right before its POST; worker B takes over and books. A must not send."""
    case, rid = await env.fresh("F2", "stale_worker")
    times = await env.structured(case, rid)

    async def pause(req, slot):
        await asyncio.sleep(2.5)

    a = Worker(env.store, env.api, owner="w-a", lease_s=1, before_post=pause)
    b = Worker(env.store, env.api, owner="w-b", lease_s=8)
    ta = asyncio.create_task(a.run_once(rid))
    await asyncio.sleep(1.6)
    rb = await b.run_once(rid)
    ra = await ta
    v = await verdict(env.oracle, case, times)
    ev = [e["kind"] for e in await events(env, rid)]
    ok = (ra["result"] == "lease_lost" and rb["result"] == "completed" and "worker.lease_lost_before_send" in ev
          and v["verdict"] == "correct" and v["changes"] == 1)
    return {"passed": ok, "stale_worker": ra, "new_worker": rb, "verdict": v}


async def check_backoff(env: Env) -> dict:
    """After a failed search the service loop does not claim the request again before its backoff ends."""
    case, rid = await env.fresh("F2", "backoff")
    times = await env.structured(case, rid)
    w = Worker(env.store, env.api, owner="w-backoff")
    await env.tox.add_latency("pg", 5000, 0, name="backoff")
    try:
        first = await w.run_once(rid, honor_backoff=True)
    finally:
        await env.tox.remove("pg", "backoff")
    again = await w.run_once(rid, honor_backoff=True)  # immediately: still inside the backoff window
    await asyncio.sleep(first.get("retry_in_s", 0) + 0.3)
    later = await w.run_once(rid, honor_backoff=True)
    v = await verdict(env.oracle, case, times)
    ok = (first["result"] == "retry_later" and first["retry_in_s"] >= 0.8 and again is None
          and later is not None and later["result"] == "completed" and v["verdict"] == "correct")
    return {"passed": ok, "first": first, "immediately_after": again, "after_backoff": later, "verdict": v}


async def check_atomic_question(env: Env) -> dict:
    """A question is stored with its turn and its email in one transaction: no question without its message."""
    case, rid = await env.fresh("F2", "atomic_question")
    await env.structured(case, rid, confirm=True)
    res = await Worker(env.store, env.api, owner="w-atomic").run_once(rid)
    req = await env.store.get(rid)
    turns = [t for t in await env.store.turns(rid) if t["role"] == "assistant"]
    notes = await env.store.notifications(rid)
    ok = (res["result"] == "needs_input" and req["question"] is not None and turns and turns[-1]["kind"] == "confirm"
          and any(n["kind"] == "confirm" and n["turn_n"] == turns[-1]["n"] for n in notes))
    return {"passed": ok, "worker": res, "question_turn": turns[-1] if turns else None,
            "notifications": [{"kind": n["kind"], "turn_n": n["turn_n"]} for n in notes]}


CHECKS = {
    "truncation": check_truncation,
    "invalid_intent": check_invalid,
    "crash_after_claim": lambda env: check_crash(env, "after_claim"),
    "crash_after_record": lambda env: check_crash(env, "after_record"),
    "crash_after_send": lambda env: check_crash(env, "after_send"),
    "lost_ack": check_lost_ack,
    "renewed_permission": check_renewed_permission,
    "correction_preserves": check_correction,
    "confirm_mode": check_confirm,
    "notifications": check_notifications,
    "fault_shapes": check_fault_shapes,
    "calendar": check_calendar,
    "superseded_attempt": check_superseded_attempt,
    "superseded_committed": check_superseded_committed,
    "correction_settles_first": check_correction_settles_first,
    "correction_waits_for_settlement": check_correction_waits_for_settlement,
    "decline_settles": check_decline_settles,
    "stale_worker": check_stale_worker,
    "backoff": check_backoff,
    "atomic_question": check_atomic_question,
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--run", default=f"faults-{dt.datetime.now():%Y%m%d-%H%M%S}")
    a = ap.parse_args()
    names = [n for n in CHECKS if not a.only or n in a.only.split(",")]
    out = Path("runs/resolve/faults") / a.run
    out.mkdir(parents=True, exist_ok=True)
    report = {"run": a.run, "checks": {}}
    async with Env(a.run) as env:
        await env.tox.reset()
        for n in names:
            try:
                res = await CHECKS[n](env)
            except Exception as exc:  # a crashed check is a failed check, recorded as such
                res = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            report["checks"][n] = res
            print(f"{n:22} {'PASS' if res['passed'] else 'FAIL'}"
                  + (f"  {res['error']}" if "error" in res else ""), flush=True)
    report["passed"] = sum(r["passed"] for r in report["checks"].values())
    report["total"] = len(report["checks"])
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(f"{report['passed']}/{report['total']} checks passed; {out / 'report.json'}")


if __name__ == "__main__":
    asyncio.run(main())
