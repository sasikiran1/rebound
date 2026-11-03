"""Live console: you type as the customer; a naive agent and Resolve act on the same world, in the open.

    scripts/resolve_live.sh [--case H15] [--split heldout] [--model muse-spark-1.3]

Lanes (switching lanes re-seeds the case's world, so both start from the same appointment and open slots):
  /naive    the same model in one shot: no booking history, no questions, no confirmation; books at once
  /resolve  Resolve in product mode: grounded interpretation, questions, corrections, confirms the exact slot
Commands:
  /outage on | off   5 s latency on FieldDesk's database link (Toxiproxy): API calls time out, requests wait
  /take              another customer books the case's contested slots (if the case file names any)
  /script            the case file's customer messages
  /status            appointment, calendar and SQL check now
  /quit
Anything else is a customer message.

The SQL check grades the booking against the case file: the slots this customer authorized (`expected.authorized`
for the opening message; `expected.final`). It is meaningful when what you type means what the case file's customer
means; the console prints the authorized slots so the audience can see what it is graded against.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from pathlib import Path

import asyncpg
import httpx

from harness import settings
from harness.fault import Toxiproxy
from harness.provider import ProviderFailed
from resolve import intent as I
from resolve.api import FieldDesk
from resolve.calendar import CalendarError, CalendarSync
from resolve.eval import cases as C
from resolve.eval.oracle import verdict
from resolve.interpret import BudgetExceeded, interpret
from resolve.models import make_model
from resolve.service import Resolve
from resolve.store import TERMINAL, Store
from resolve.worker import Worker

RED, GREEN, YELLOW, BLUE, DIM, BOLD, OFF = ("\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[2m", "\033[1m",
                                            "\033[0m")
OUTAGE = "live_outage"


def out(tag: str, text: str, color: str = "") -> None:
    print(f"\r{color}{tag:>10}{OFF}  {text}", flush=True)


def slot_label(case: C.Case, key: str) -> str:
    s = case.slots[C.norm_key(key)]
    d = s["date"] if isinstance(s["date"], dt.date) else dt.date.fromisoformat(str(s["date"]))
    return f"{d:%a %b} {d.day} {s['time']} with {case.techs[s['tech']]['name']}"


class Live:
    def __init__(self, case: C.Case, model_name: str, max_calls: int, max_cost: float):
        self.case, self.model_name = case, model_name
        self.model = make_model(model_name, Path("runs/resolve/live/raw"), max_calls, max_cost)
        self.lane: str | None = None
        self.rid: str | None = None
        self.n = 0
        self.outage = False
        self.seen_turns = 0
        self.reported = True
        self.t_open: dt.datetime | None = None

    async def __aenter__(self):
        self.admin = await asyncpg.connect(settings.admin_dsn(settings.TRIAL_DB))
        self.oracle = await asyncpg.connect(settings.oracle_dsn())
        self.store = await Store(settings.admin_dsn(settings.TRIAL_DB)).open()
        self.api = FieldDesk(settings.FIELDDESK_URL, actor="resolve-live")
        self.svc = Resolve(self.store, self.api, self.model, C.TODAY, C.HORIZON_DAYS)
        self.worker = Worker(self.store, self.api, owner="worker-live")
        self.tox_http = httpx.AsyncClient()
        self.tox = Toxiproxy(self.tox_http)
        self.calendar = CalendarSync.from_env(self.store, self.api)
        self.names = {t["id"]: t["name"] for t in self.case.techs.values()}
        self.roster = [{"technician_id": t["id"], "name": t["name"]} for t in self.case.techs.values()]
        return self

    async def __aexit__(self, *exc):
        try:
            await self.tox.reset()
        except httpx.HTTPError:
            pass
        await self.tox_http.aclose()
        if self.calendar is not None:
            await self.calendar.close()
        await self.api.close()
        await self.store.close()
        await self.admin.close()
        await self.oracle.close()
        if hasattr(self.model.client, "close"):
            await self.model.client.close()

    # --- world -----------------------------------------------------------------------------------------------------
    def banner(self) -> None:
        c = self.case
        print(f"\n{BOLD}Customer {c.raw['customer']}{OFF}: current appointment {slot_label(c, c.raw['current'])}"
              if C.norm_key(c.raw["current"]) in c.slots else
              f"\n{BOLD}Customer {c.raw['customer']}{OFF}: current appointment {c.raw['current']}")
        print(f"{DIM}Open slots:{OFF} " + "; ".join(slot_label(c, k) for k in c.raw["open"]))
        auth = c.expected["authorized"][0] if c.expected["authorized"] else []
        print(f"{DIM}Graded against (case file): authorized "
              + ("; ".join(slot_label(c, k) for k in auth) or "nothing: no booking is correct")
              + f"; script: {' / '.join(c.messages)}{OFF}")

    async def calendar_line(self) -> str:
        if self.calendar is None:
            return "no calendar configured"
        held = []
        for tid, name in self.names.items():
            body = await self.calendar.caldav.get_event(tid, self.case.appointment_id) or ""
            start = next((x[8:] for x in body.splitlines() if x.startswith("DTSTART:")), None)
            if start:
                held.append(f"{name}'s calendar {dt.datetime.strptime(start, '%Y%m%dT%H%M%S'):%a %b %d %H:%M}")
        return "; ".join(held) or "in no technician's calendar"

    async def start_lane(self, lane: str) -> None:
        if self.outage:
            out("!", "turn the outage off before switching lanes (the world is re-seeded through FieldDesk)", YELLOW)
            return
        self.lane, self.n = lane, self.n + 1
        self.rid = f"live:{self.case.id}:{lane}:{dt.datetime.now():%H%M%S}"
        await C.seed(self.admin, self.case)
        await self.store.reset(self.rid)
        self.facts = await self.svc.load_facts(self.case.customer_id, self.case.appointment_id)
        self.seen_turns, self.reported, self.t_open, self.texts = 0, True, None, []
        title = ("NAIVE AGENT: same model, one shot, books at once, never asks" if lane == "naive" else
                 "RESOLVE: grounded rules, asks when unsure, confirms the exact slot before writing")
        print(f"\n{BOLD}{RED if lane == 'naive' else GREEN}== {title} =={OFF}  {DIM}request {self.rid}{OFF}")
        self.banner()
        if self.calendar is not None:
            try:
                await self.calendar.mirror(self.case.appointment_id, self.roster)
                out("calendar", await self.calendar_line(), BLUE)
            except (CalendarError, httpx.HTTPError) as exc:
                out("calendar", f"unreachable ({type(exc).__name__}); continuing without it", YELLOW)
                await self.calendar.close()
                self.calendar = None

    # --- customer --------------------------------------------------------------------------------------------------
    async def customer(self, text: str) -> None:
        if self.lane is None:
            out("!", "pick a lane first: /naive or /resolve", YELLOW)
            return
        req = await self.store.get(self.rid)
        if req is not None and req["state"] in TERMINAL:
            out("!", f"this request is {req['state']}; /naive or /resolve starts again", YELLOW)
            return
        self.texts.append(text)
        if self.t_open is None:
            self.t_open = await self.oracle.fetchval("SELECT clock_timestamp()")
        calls = len(self.model.log.calls)
        try:
            if self.lane == "naive":
                if req is not None:
                    out("naive", "already acted on the first message; it does not hold a conversation", DIM)
                    return
                await self.store.create(self.rid, self.case.customer_id, self.case.appointment_id, self.case.region,
                                        self.case.service, C.TODAY, C.HORIZON_DAYS, self.facts)
                await self.store.add_turn(self.rid, "customer", text, "message")
                res = await interpret(self.model, {**self.facts, "history": []},
                                      [{"role": "customer", "text": text}], repair=False)
                await self.store.event(self.rid, "interpret", attempts=res.attempts, problems=res.problems,
                                       intent=res.value)
                if res.value is None:
                    out("naive", "its reading was invalid, and it has no repair turn: gave up ("
                        + "; ".join(res.problems)[:200] + ")", RED)
                    await self.store.set_state(self.rid, "unresolved", detail="; ".join(res.problems)[:1000])
                else:
                    out("naive", "understood: " + I.explain(res.value, self.names), RED)
                    await self.store.approve(self.rid, res.value, {"turn": 0, "by": "customer", "text": text}, None)
            elif req is None:
                await self.svc.open(self.rid, self.case.customer_id, self.case.appointment_id, self.case.region,
                                    self.case.service, text, self.facts, confirm=True)
                v = await self.store.get(self.rid)
                if v["version"]:
                    out("resolve", "understood: " + I.explain(await self.store.intent(self.rid, v["version"]),
                                                              self.names), GREEN)
            else:
                res = await self.svc.customer_says(self.rid, text)
                if res.get("diff"):
                    out("resolve", f"changed version {res.get('version', '')}: {res['diff']}", GREEN)
        except BudgetExceeded as exc:
            out("!", f"model budget reached: {exc}", YELLOW)
        except ProviderFailed as exc:  # the model call failed; nothing was approved, the request is unchanged
            out("!", f"model call failed ({str(exc)[:120]}); type the message again", YELLOW)
            self.texts.pop()
            if req is None:  # the first message: start the request afresh
                await self.store.reset(self.rid)
                self.seen_turns, self.t_open = 0, None
        new = self.model.log.calls[calls:]
        if new:
            out("model", f"{len(new)} call(s), {sum(c['latency_ms'] for c in new) / 1000:.1f} s; session "
                f"{len(self.model.log.calls)} calls, ${self.model.log.cost:.4f}", DIM)
        self.reported = False

    # --- worker, in the background ---------------------------------------------------------------------------------
    async def pump(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as exc:  # keep the console alive; show what happened
                out("!", f"{type(exc).__name__}: {exc}", YELLOW)
                await asyncio.sleep(1)

    async def _tick(self) -> None:
        rid = self.rid
        req = await self.store.get(rid) if rid else None
        if req is None:
            await asyncio.sleep(0.3)
            return
        turns = await self.store.turns(rid)
        for t in turns[self.seen_turns:]:
            if t["role"] == "assistant":
                out(self.lane, t["text"], BOLD)
        self.seen_turns = len(turns)
        if req["state"] == "ready":
            res = await self.worker.run_once(rid)
            if res and res.get("result") == "retry_later":
                out("worker", f"FieldDesk did not answer ({res['reason']}); the request waits and retries", YELLOW)
                await asyncio.sleep(2)
            elif res:
                out("worker", res["result"] + (f" ({res.get('how')})" if res.get("how") else ""), DIM)
            return
        if req["state"] == "needs_input" and self.lane == "naive" and not self.reported:
            await self.store.set_state(rid, "unresolved", detail="one-shot agent cannot ask the customer")
            out("naive", "would have needed to ask, but cannot: gave up", RED)
            return
        if req["state"] in TERMINAL and not self.reported:
            self.reported = True
            await self.report()
        await asyncio.sleep(0.3)

    async def report(self) -> None:
        if self.calendar is not None:
            synced = await self.calendar.drain(request_id=self.rid)
            out("calendar", await self.calendar_line() + ("  (synced)" if synced["synced"] else ""), BLUE)
        times = [self.t_open] if self.t_open else []
        v = await verdict(self.oracle, self.case, times)
        good = v["verdict"] in ("correct", "correct_no_booking")
        final = (slot_label(self.case, v["final_slot"]) if v["final_slot"] in self.case.slots
                 else "unchanged (current appointment)")
        text = {"correct": "CORRECT", "correct_no_booking": "CORRECT (no booking)", "unauthorized": "UNAUTHORIZED",
                "wrong_slot": "WRONG SLOT", "missed_booking": "NOTHING BOOKED",
                "duplicate_effect": "DOUBLE BOOKING"}[v["verdict"]]
        why = ("the customer never authorized that slot" if v["unauthorized"] else
               f"{v['changes']} change row(s)" + (", the slot the customer wanted" if good and v["changes"] else ""))
        out("SQL check", f"{BOLD}{text}{OFF}{GREEN if good else RED}: appointment is {final}; {why}",
            GREEN if good else RED)

    # --- commands --------------------------------------------------------------------------------------------------
    async def command(self, line: str) -> bool:
        cmd = line.split()
        if cmd[0] == "/quit":
            return False
        if cmd[0] in ("/naive", "/resolve"):
            await self.start_lane(cmd[0][1:])
        elif cmd[0] == "/outage" and len(cmd) == 2 and cmd[1] in ("on", "off"):
            if cmd[1] == "on" and not self.outage:
                await self.tox.add_latency("pg", 5000, 0, name=OUTAGE)
                self.outage = True
                out("OUTAGE", "FieldDesk's database link now has 5 s latency: its API calls time out", YELLOW)
            elif cmd[1] == "off" and self.outage:
                await self.tox.remove("pg", OUTAGE)
                self.outage = False
                out("recovered", "latency removed", GREEN)
        elif cmd[0] == "/take":
            if not self.case.taken:
                out("!", "this case file names no contested slots", YELLOW)
            else:
                await C.take(self.admin, self.case)
                out("elsewhere", "another customer booked " + "; ".join(slot_label(self.case, k)
                                                                        for k in self.case.taken), YELLOW)
        elif cmd[0] == "/script":
            for m in self.case.messages + self.case.replies:
                print(f"           {DIM}{m}{OFF}")
        elif cmd[0] == "/status":
            req = await self.store.get(self.rid) if self.rid else None
            out("status", f"lane={self.lane} state={req and req['state']} outage={self.outage}")
            if req is not None and req["state"] in TERMINAL:
                await self.report()
        else:
            out("!", "unknown command; see the top of resolve/eval/live.py", YELLOW)
        return True


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="H15")
    ap.add_argument("--split", default="heldout")
    ap.add_argument("--model", default="qwen3-14b-16k:latest")
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--max-cost", type=float, default=0.50)
    a = ap.parse_args()
    case = next(c for c in C.load(C.CASE_DIR / f"{a.split}.yaml") if c.id == a.case)
    async with Live(case, a.model, a.max_calls, a.max_cost) as live:
        await live.tox.reset()
        print(f"{BOLD}Rebound live console{OFF}  model {a.model}  case {case.id}  "
              f"{DIM}(synthetic fixture; you are the customer){OFF}")
        print(f"{DIM}/naive  /resolve  /outage on|off  /take  /script  /status  /quit{OFF}")
        pump = asyncio.create_task(live.pump())
        try:
            while True:
                line = (await asyncio.to_thread(input, "you> ")).strip()
                if not line:
                    continue
                if line.startswith("/"):
                    if not await live.command(line):
                        break
                else:
                    await live.customer(line)
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            pump.cancel()


if __name__ == "__main__":
    asyncio.run(main())
