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
        refused = await notifier.drain()
        queued = (await env.store.notifications(rid))[0]
        sink.refuse = False
        await asyncio.sleep(5.5)  # first retry is due 5 s after a failure
        retried = await notifier.drain()
        times.append(await env.now())
        await Resolve(env.store, env.api, None, C.TODAY, C.HORIZON_DAYS).customer_says(rid, "Yes, please book it.")
        await w.run_once(rid)
        final = await notifier.drain()
        rows = await env.store.notifications(rid)
        v = await verdict(env.oracle, case, times)
    finally:
        await sink.stop()
    # The notifier drains every due message, other requests' included (several checks share this fixture's
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
