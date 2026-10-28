"""Constraint-semantics reproduction with live local Ollama agents (no fault, no gate, no background traffic).

Usage: python scripts/ollama_semantic.py <out_dir> <plan.json>

Every case runs the repository's own agent loop (harness.agent.run_goal) against a freshly cloned database and a
freshly restarted FieldDesk. Scenarios are built here from SQL on fielddesk_template, independently of any earlier
package. Business outcomes are judged by `scenario_check` from final database state, separately from the original
date-only oracle (whose verdict is recorded beside it).

Cases (each built for task set A, reproducing earlier work, and for held-out set B):
  LATE  - feasible task plus an explicit 15:00-15:59 start requirement
  TECH  - feasible 3-day task plus "only technician T" (T = highest technician id among candidates)
  FULL  - an infeasible task (no qualified free slot): deferral is correct
  RACE  - two live agents, different customers, the same single acceptable slot. CONTROLLED INTERLEAVING: each agent
          waits at a barrier right after its first availability result that contains the target slot, so both have
          seen it before either continues. Server responses are never altered.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import sys
import time
from pathlib import Path

import asyncpg

sys.path.insert(0, ".")
from harness import dbadmin, settings, sut  # noqa: E402
from harness.adapter import GoalState, ToolAdapter, load_tools, new_http_client  # noqa: E402
from harness.agent import AgentConfig, run_goal  # noqa: E402
from harness.events import EventLog  # noqa: E402
from harness.manifest import load  # noqa: E402
from harness.ollama import OllamaClient  # noqa: E402
from harness.provider import Provider  # noqa: E402
from oracle.verdicts import verdict_for  # noqa: E402

MANIFEST = "manifests/exploratory/explore_v1.yaml"
LATE_TEXT = (" The customer is at work in the morning: the new appointment MUST start at 15:00 or later, before 16:00."
             " Do not book a morning appointment. If no slot satisfies this, defer.")
CAND = """SELECT s.id, s.technician_id, s.starts_at FROM slots s
  JOIN technician_regions tr ON tr.technician_id = s.technician_id JOIN regions r ON r.id = tr.region_id
  JOIN technician_skills ts ON ts.technician_id = s.technician_id JOIN service_types st ON st.skill_id = ts.skill_id
  WHERE r.code = $1 AND st.code = $2 AND s.slot_date BETWEEN $3::date AND $4::date
  AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.slot_id = s.id AND a.status = 'scheduled')
  ORDER BY s.starts_at, s.technician_id"""


def tasks(name: str) -> list[dict]:
    return [json.loads(x) for x in Path(f"data/tasks/{name}").read_text().splitlines()]


async def candidates(conn, t: dict) -> list[dict]:
    rows = await conn.fetch(CAND, t["customer_region"], t["service"], dt.date.fromisoformat(t["d1"]),
                            dt.date.fromisoformat(t["d2"]))
    return [dict(r) for r in rows]


def satisfies(t: dict, slot: dict) -> bool:
    c = t.get("constraint") or {}
    if c.get("kind") == "start_hour":
        return c["from_h"] <= slot["starts_at"].hour < c["to_h"]
    if c.get("kind") == "technician":
        return slot["technician_id"] == c["technician_id"]
    if c.get("kind") == "exact_slot":
        return slot["id"] == c["slot_id"]
    return True


class RecordingAdapter(ToolAdapter):
    """Keeps every tool result the model received (for the visibility check). Optional race barrier."""

    def __init__(self, *a, barrier: asyncio.Barrier | None = None, target: int | None = None, **kw):
        super().__init__(*a, **kw)
        self.seen: list[dict] = []
        self.barrier, self.target, self.waited = barrier, target, False

    async def call(self, scope, state, name, raw_args, *a, **kw):
        result = await super().call(scope, state, name, raw_args, *a, **kw)
        self.seen.append({"tool": name, "args": raw_args, "result": result})
        if (self.barrier and not self.waited and name in ("check_day_availability", "search_availability")
                and any(s.get("slot_id") == self.target for s in result.get("slots", []))):
            self.waited = True
            self.events.emit("race.observed_target", goal_id=scope.goal_id, slot_id=self.target)
            try:
                await asyncio.wait_for(self.barrier.wait(), 120)
            except (TimeoutError, asyncio.BrokenBarrierError):
                self.events.emit("race.barrier_incomplete", goal_id=scope.goal_id)
        return result


async def build_cases(conn) -> dict:
    A, B = tasks("tasks_dev_A.jsonl"), tasks("tasks_eval_B.jsonl")
    http = new_http_client()
    probe_events = EventLog(Path("/tmp/rebound_probe"), trial_id="probe")
    probe = RecordingAdapter(settings.FIELDDESK_URL, http, load_tools(), probe_events)
    cases: dict[str, dict] = {}

    async def day_visible(t):  # what the day lookup shows the model (fixture witness, no model involved)
        from harness.adapter import GoalScope
        r = await probe.call(GoalScope("probe", 1, t["customer_id"], t["appointment_id"], t["allowed_regions"]),
                             GoalState(), "check_day_availability",
                             {"region": t["customer_region"], "date": t["d1"], "service": t["service"]})
        return [s["slot_id"] for s in r.get("slots", [])], r.get("count"), r.get("truncated")

    async def late(pool, tag, fixed=None):
        for t in pool:
            if fixed and t["task_id"] != fixed:
                continue
            if t["kind"] != "narrow_1day" or t["expected"] != "reschedule":
                continue
            c = await candidates(conn, t)
            ok = [x for x in c if 15 <= x["starts_at"].hour < 16]
            vis, count, trunc = await day_visible(t)
            if ok and not ({x["id"] for x in ok} & set(vis)):
                t = copy.deepcopy(t)
                t.update(task_id=f"LATE_{tag}", base_task=t["task_id"], goal_text=t["goal_text"] + LATE_TEXT,
                         constraint={"kind": "start_hour", "from_h": 15, "to_h": 16})
                return t, {"feasible_ids": [x["id"] for x in ok], "candidates": len(c), "day_lookup_count": count,
                           "day_lookup_visible": len(vis), "day_lookup_truncated": trunc}
        raise RuntimeError(f"no LATE fixture in {tag}")

    async def tech(pool, tag):
        t = next(x for x in pool if x["kind"] == "flexible_3day" and x["expected"] == "reschedule")
        c = await candidates(conn, t)
        tid = max(x["technician_id"] for x in c)
        ok = [x for x in c if x["technician_id"] == tid]
        t = copy.deepcopy(t)
        t.update(task_id=f"TECH_{tag}", base_task=t["task_id"], constraint={"kind": "technician", "technician_id": tid},
                 goal_text=t["goal_text"] + f" The customer requires technician {tid}, who previously serviced the "
                 "equipment. Only that technician is acceptable; otherwise defer without changing the appointment.")
        return t, {"feasible_ids": [x["id"] for x in ok], "candidates": len(c)}

    async def full(pool, tag):
        t = copy.deepcopy(next(x for x in pool if x["expected"] == "defer"))
        c = await candidates(conn, t)
        t.update(task_id=f"FULL_{tag}", base_task=t["task_id"])
        return t, {"feasible_ids": [x["id"] for x in c], "candidates": len(c)}

    async def race(base, tag):
        c = await candidates(conn, base)
        target = c[0]
        other = await conn.fetchrow(
            """SELECT a.id AS appointment_id, a.customer_id, s.slot_date::text AS current_date FROM appointments a
               JOIN customers cu ON cu.id = a.customer_id JOIN regions r ON r.id = cu.region_id
               JOIN service_types st ON st.id = a.service_type_id JOIN slots s ON s.id = a.slot_id
               WHERE a.status = 'scheduled' AND r.code = $1 AND st.code = $2 AND a.customer_id <> $3
               AND (SELECT count(*) FROM appointments z WHERE z.customer_id = a.customer_id AND z.status = 'scheduled'
                    AND z.service_type_id = a.service_type_id) = 1
               ORDER BY a.id LIMIT 1""", base["customer_region"], base["service"], base["customer_id"])
        out = []
        for i, src in enumerate([{}, dict(other)], 1):
            t = copy.deepcopy(base)
            t.update(src)
            t.update(task_id=f"RACE_{tag}_{i}", base_task=base["task_id"],
                     constraint={"kind": "exact_slot", "slot_id": target["id"]},
                     goal_text=(f"Customer {t['customer_id']} wants their {t['service']} appointment on "
                                f"{t['current_date']} moved to {target['starts_at']:%Y-%m-%d %H:%M} with technician "
                                f"{target['technician_id']}. Only that exact date, start time and technician are "
                                "acceptable. If it is no longer available, defer and make no change. Keep the same "
                                "service and regional qualification."))
            out.append(t)
        return out, {"target": {k: str(v) for k, v in target.items()}, "feasible_ids": [target["id"]]}

    for tag, pool, fixed in (("A", A, "A0001"), ("B", B, None)):
        cases[f"LATE_{tag}"] = await late(pool, tag, fixed)
        cases[f"TECH_{tag}"] = await tech(pool, tag)
        cases[f"FULL_{tag}"] = await full(pool, tag)
        base = next(t for t in pool if t["task_id"] == cases[f"LATE_{tag}"][0]["base_task"])
        cases[f"RACE_{tag}"] = await race(base, tag)
    await http.aclose()
    probe_events.close()
    return cases


async def fresh_sut(m) -> dict:
    async with new_http_client() as c:
        toxics = (await c.get(settings.TOXIPROXY_URL + "/proxies/pg/toxics")).json()
    if toxics:
        raise RuntimeError(f"toxics present: {toxics}")
    fp = await dbadmin.clone_trial_db()
    if fp["checksum"] != m.data.checksum:
        raise RuntimeError("template checksum mismatch")
    await asyncio.to_thread(sut.restart_fielddesk, sut.fielddesk_env(m, False))
    return fp


async def scenario_check(conn, t: dict, seen: list[dict], outcome, feasible_before: list[int], cfg_ctx: int,
                         cfg_predict: int, model_calls: list[dict]) -> dict:
    changes = await conn.fetch("SELECT to_slot_id FROM appointment_changes WHERE appointment_id = $1 ORDER BY id",
                               t["appointment_id"])
    booked = None
    if changes:
        r = await conn.fetchrow(
            """SELECT s.id, s.technician_id, s.starts_at, s.slot_date,
                 EXISTS (SELECT 1 FROM technician_skills ts JOIN service_types st ON st.skill_id = ts.skill_id
                         WHERE ts.technician_id = s.technician_id AND st.code = $2) AS skill,
                 EXISTS (SELECT 1 FROM technician_regions tr JOIN regions g ON g.id = tr.region_id
                         WHERE tr.technician_id = s.technician_id AND g.code = $3) AS region,
                 (SELECT count(*) FROM appointments a WHERE a.slot_id = s.id AND a.status = 'scheduled') AS occupants
               FROM slots s WHERE s.id = $1""", changes[-1]["to_slot_id"], t["service"], t["customer_region"])
        booked = dict(r)
    feasible_after = [x["id"] for x in await candidates(conn, t) if satisfies(t, x)]
    sat_ids = set(feasible_before)
    visible = sorted({s["slot_id"] for e in seen for s in (e["result"].get("slots") or []) if s.get("slot_id") in sat_ids})
    tool_errors = [e["result"]["error"]["type"] for e in seen if "error" in e["result"]]
    ctx_exhausted = any(c.get("done_reason") == "length" and (c.get("context_used") or 0) >= cfg_ctx - 8
                        for c in model_calls)
    out_limit = any(c.get("done_reason") == "length" and (c.get("output_tokens") or 0) >= cfg_predict for c in model_calls)
    names = [e["tool"] for e in seen]
    readback = "reschedule_appointment" in names and "get_appointment" in names[names.index("reschedule_appointment"):]
    violated = []
    if booked:
        d1, d2 = dt.date.fromisoformat(t["d1"]), dt.date.fromisoformat(t["d2"])
        if not d1 <= booked["slot_date"] <= d2:
            violated.append("date_window")
        if not booked["skill"]:
            violated.append("skill")
        if not booked["region"]:
            violated.append("region")
        if booked["occupants"] != 1:
            violated.append("double_booked")
        if not satisfies(t, booked):
            violated.append(t["constraint"]["kind"])
    if len(changes) > 1:
        cls = "duplicate_effect"
    elif changes:
        cls = "correct_booking" if not violated else "incorrect_booking"
    elif outcome.end_reason == "deferred":
        cls = "correct_deferral" if not feasible_after else "wrong_deferral_feasible"
    elif outcome.end_reason == "provider_failed":
        cls = "model_runner_failure"
    elif ctx_exhausted:
        cls = "feasible_not_completed:context_window_exhausted"
    elif out_limit:
        cls = "feasible_not_completed:output_token_limit"
    elif outcome.end_reason == "limit_reached":
        cls = f"feasible_not_completed:{outcome.limit}_limit"
    else:
        cls = "feasible_not_completed:" + outcome.end_reason
    return {"class": cls, "violated": violated, "booked": {k: str(v) for k, v in booked.items()} if booked else None,
            "changes": len(changes), "feasible_before": len(feasible_before), "feasible_after": len(feasible_after),
            "satisfying_slots_visible_to_model": visible, "tool_errors": tool_errors,
            "recovered_tool_error": bool(tool_errors) and cls in ("correct_booking", "correct_deferral"),
            "context_exhausted": ctx_exhausted, "output_limit": out_limit, "readback_after_write": readback,
            "deferred_reason": outcome.deferred_reason}


def model_calls_of(events_dir: Path, goal_id: str) -> list[dict]:
    rows = []
    for line in (events_dir / "model_calls.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e.get("event_type") == "model.call" and e.get("goal_id") == goal_id:
            rows.append(e)
    return rows


async def run_case(m, model, setting, case_name, case, out: Path, cases_meta: dict) -> list[dict]:
    opts, predict, think = setting["options"], setting["num_predict"], setting["think"]
    if model.startswith("qwen2.5"):  # no thinking mode; the earlier package also omitted the field for it
        think = None
    is_race = case_name.startswith("RACE")
    goal_tasks = case[0] if is_race else [case[0]]
    folder = out / model.replace(":", "_") / setting["name"] / case_name
    folder.mkdir(parents=True)
    await fresh_sut(m)
    events = EventLog(folder, experiment_id="ollama-semantic", trial_id=f"sem_{setting['name']}", condition="agent",
                      gate="off", config_hash=None)
    client = OllamaClient(opts, think=think, raw_dir=folder / "ollama_raw")
    provider = Provider(client)
    tools = load_tools(setting.get("tools", "prompts/tools_v1.json"))
    cfg = AgentConfig(model=model, max_output_tokens=predict, continuation="full_history", max_model_calls=16,
                      max_tool_calls=14, goal_deadline_s=240)
    barrier = asyncio.Barrier(2) if is_race else None
    target = case[1]["feasible_ids"][0] if is_race else None
    conn = await asyncpg.connect(settings.oracle_dsn())
    before = {t["task_id"]: [x["id"] for x in await candidates(conn, t) if satisfies(t, x)] for t in goal_tasks}
    await conn.close()
    async with new_http_client() as http:
        adapters = [RecordingAdapter(settings.FIELDDESK_URL, http, tools, events, barrier=barrier, target=target)
                    for _ in goal_tasks]
        t0 = time.monotonic()
        outs = await asyncio.gather(*(run_goal(t, i, f"sem_{setting['name']}", provider, adapters[i - 1], events,
                                               tools, cfg) for i, t in enumerate(goal_tasks, 1)))
    events.close()
    await client.close()
    conn = await asyncpg.connect(settings.oracle_dsn())
    rows = []
    try:
        for i, (t, o, ad) in enumerate(zip(goal_tasks, outs, adapters), 1):
            mc = model_calls_of(folder, o.goal_id)
            check = await scenario_check(conn, t, ad.seen, o, before[t["task_id"]], opts["num_ctx"], predict, mc)
            if is_race and check["class"] == "correct_deferral":
                owner = await conn.fetchval("SELECT id FROM appointments WHERE slot_id = $1 AND status = 'scheduled'",
                                            target)
                check["slot_owner_appointment"] = owner
                if owner == t["appointment_id"] or owner is None:
                    check["class"] = "wrong_deferral_feasible"
            orig = await verdict_for(conn, {k: v for k, v in t.items() if k != "constraint"}, o.end_reason)
            rows.append({"model": model, "setting": setting["name"], "options": opts, "num_predict": predict,
                         "case": case_name, "task_id": t["task_id"], "base_task": t["base_task"],
                         "end_reason": o.end_reason, "limit": o.limit, "model_calls": o.model_calls,
                         "tool_calls": o.tool_calls, "http_attempts": o.attempts,
                         "duration_s": round(time.monotonic() - t0, 1),
                         "tool_sequence": [(e["tool"], e["result"]["error"]["type"] if "error" in e["result"] else "ok")
                                           for e in ad.seen],
                         "model_latency_s": [round((c.get("latency_ms") or 0) / 1000, 2) for c in mc],
                         "done_reasons": [c.get("done_reason") for c in mc],
                         "max_context_used": max((c.get("context_used") or 0) for c in mc) if mc else None,
                         "original_oracle": orig["verdict"], "scenario": check})
    finally:
        await conn.close()
    (folder / "result.json").write_text(json.dumps(rows, indent=2, default=str))
    for r in rows:
        s = r["scenario"]
        print(json.dumps({"model": model, "setting": setting["name"], "task": r["task_id"], "class": s["class"],
                          "orig": r["original_oracle"], "booked": s["booked"] and s["booked"]["starts_at"],
                          "visible_ok": len(s["satisfying_slots_visible_to_model"]), "tools": r["tool_sequence"],
                          "ctx": r["max_context_used"]}), flush=True)
    return rows


async def main() -> None:
    out, plan = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
    out.mkdir(parents=True, exist_ok=False)
    m = load(MANIFEST)
    await fresh_sut(m)
    conn = await asyncpg.connect(settings.oracle_dsn("fielddesk_template"))
    cases = await build_cases(conn)
    await conn.close()
    (out / "cases.json").write_text(json.dumps(cases, indent=2, default=str))
    (out / "plan.json").write_text(json.dumps(plan, indent=2))
    for name, (t, meta) in cases.items():
        print("CASE", name, json.dumps({k: v for k, v in meta.items() if k != "feasible_ids"}, default=str),
              "feasible", len(meta["feasible_ids"]), flush=True)
    rows = []
    for run in plan["runs"]:
        for model in run["models"]:
            for setting in run["settings"]:
                for case_name in run["cases"]:
                    try:
                        rows += await run_case(m, model, setting, case_name, cases[case_name], out, cases)
                    except Exception as exc:  # noqa: BLE001 - recorded, never hidden
                        print("RUN_FAILED", model, setting["name"], case_name, repr(exc)[:500], flush=True)
                        rows.append({"model": model, "setting": setting["name"], "case": case_name,
                                     "run_failed": repr(exc)[:1000]})
                    (out / "results.json").write_text(json.dumps(rows, indent=2, default=str))
    print("DONE", out, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
