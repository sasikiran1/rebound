"""Hardened agent: requirement enforcement in the adapter; bug-hunter scenario validation."""

import json
import sys
from pathlib import Path

import httpx

from harness.adapter import GoalScope, GoalState, ToolAdapter, load_tools, requirement_problem
from harness.events import EventLog

sys.path.insert(0, "scripts")
from bug_hunt import parse_scenario  # noqa: E402

SLOT = {"slot_id": 5, "starts_at": "2026-11-11T08:00", "technician_id": 19}
SCOPE = GoalScope("tr", 1, 11, 101, ["R03"])


def test_requirement_problem():
    assert "record_requirements" in requirement_problem(None, SLOT)
    assert requirement_problem({}, SLOT) is None  # recorded: the customer asked for nothing extra
    assert "not returned" in requirement_problem({}, None)
    assert "before the required earliest" in requirement_problem({"earliest_start": "15:00"}, SLOT)
    assert requirement_problem({"earliest_start": "08:00"}, SLOT) is None
    assert "latest start" in requirement_problem({"latest_start": "08:00"}, SLOT)  # must start strictly before
    assert "requires 384" in requirement_problem({"technician_id": 384}, SLOT)
    assert requirement_problem({"technician_id": 19, "latest_start": "10:00"}, SLOT) is None


def make(tmp_path, tools_file, calls):
    def backend(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if "availability" in req.url.path:
            return httpx.Response(200, json={"count": 1, "slots": [SLOT]})
        return httpx.Response(200, json={"change_id": 9})
    http = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    return ToolAdapter("http://fd.test", http, load_tools(tools_file), EventLog(tmp_path, trial_id="t"))


async def test_v3_blocks_a_violating_booking_without_touching_the_api(tmp_path):
    calls: list = []
    a, st = make(tmp_path, "prompts/tools_v3.json", calls), GoalState()
    book = {"appointment_id": 101, "slot_id": 5}
    r = await a.call(SCOPE, st, "reschedule_appointment", book)
    assert r["error"]["type"] == "requirement_violation" and calls == []  # nothing recorded yet
    await a.call(SCOPE, st, "record_requirements", {"earliest_start": "15:00"})
    await a.call(SCOPE, st, "check_day_availability", {"region": "R03", "date": "2026-11-11", "service": "s"})
    r = await a.call(SCOPE, st, "reschedule_appointment", book)
    assert r["error"]["type"] == "requirement_violation" and st.requirement_blocks == 2 and not st.write_applied
    assert not any("reschedule" in p for p in calls)


async def test_v3_allows_a_booking_that_meets_the_requirements(tmp_path):
    calls: list = []
    a, st = make(tmp_path, "prompts/tools_v3.json", calls), GoalState()
    await a.call(SCOPE, st, "record_requirements", {"earliest_start": "08:00"})
    await a.call(SCOPE, st, "check_day_availability", {"region": "R03", "date": "2026-11-11", "service": "s"})
    r = await a.call(SCOPE, st, "reschedule_appointment", {"appointment_id": 101, "slot_id": 5})
    assert r == {"change_id": 9} and st.write_applied


async def test_original_tools_have_no_requirement_check(tmp_path):
    calls: list = []
    a, st = make(tmp_path, "prompts/tools_v1.json", calls), GoalState()
    r = await a.call(SCOPE, st, "reschedule_appointment", {"appointment_id": 101, "slot_id": 5})
    assert r == {"change_id": 9}


def test_parse_scenario_accepts_only_the_allowed_shapes():
    c = {"kind": "start_hour", "from_h": 15, "to_h": 16}
    good = {"message": "after 3 please", "constraint": c, "why_hard": "x"}
    assert parse_scenario("Sure:\n" + json.dumps(good))["constraint"]["from_h"] == 15
    tech = {"message": "only tech 7", "constraint": {"kind": "technician", "technician_id": 7}}
    assert parse_scenario(json.dumps(tech))["constraint"]["technician_id"] == 7
    assert parse_scenario("no json here") is None
    assert parse_scenario('{"message": "x"') is None
    assert parse_scenario(json.dumps({**good, "constraint": {"kind": "weekday"}})) is None
    assert parse_scenario(json.dumps({**good, "constraint": {"kind": "start_hour", "from_h": 16, "to_h": 15}})) is None
    assert parse_scenario(json.dumps({**good, "message": "  "})) is None


def test_task_set_keeps_the_answer_out_of_the_message_and_is_balanced():
    tasks = [json.loads(x) for x in Path("data/tasks/tasks_messy_v1.jsonl").read_text().splitlines()]
    assert len(tasks) == 36 and {t["split"] for t in tasks} == {"dev", "held_out"}
    assert all(t["feasible"] == 0 for t in tasks if t["expected"] == "defer")
    assert all(t["feasible"] > 0 for t in tasks if t["expected"] == "reschedule")
