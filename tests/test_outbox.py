"""Wait tool outside the tool-call budget; store_intent handoff; the outbox worker's slot choice."""

import datetime as dt
import json

import httpx

from harness.adapter import GoalScope, GoalState, ToolAdapter, load_tools
from harness.events import EventLog
from harness.outbox import WORKER_TOOLS, complete
from oracle.verdicts import Facts, classify

SCOPE = GoalScope("tr", 3, 11, 101, ["R03"])
APPT = {"appointment_id": 101, "customer_id": 11, "status": "scheduled", "service": "hvac_repair"}


class FakeOutbox:
    def __init__(self):
        self.rows = []

    async def put(self, scope, intent):
        self.rows.append((scope.goal_id, scope.appointment_id, intent))
        return len(self.rows)


def adapter(tmp_path, tools_file, backend, outbox=None):
    async def no_sleep(_s):
        return None

    return ToolAdapter("http://fd.test", httpx.AsyncClient(transport=httpx.MockTransport(backend)),
                       load_tools(tools_file), EventLog(tmp_path, trial_id="t"), sleep=no_sleep, outbox=outbox)


async def test_wait_does_not_spend_the_tool_budget(tmp_path):
    a = adapter(tmp_path, "prompts/tools_v4_intent.json", lambda r: httpx.Response(200, json=APPT))
    st = GoalState()
    for _ in range(5):
        await a.call(SCOPE, st, "wait", {"seconds": 2})
    await a.call(SCOPE, st, "get_appointment", {"appointment_id": 101})
    assert (st.waits, st.tool_calls, st.ops) == (5, 1, 6)


async def test_store_intent_writes_the_scope_not_the_model_and_is_refused_without_an_outbox(tmp_path):
    intent = {"date_from": "2026-11-09", "date_to": "2026-11-11", "earliest_start": "15:00",
              "customer_words": "afternoons only, from 3"}
    a = adapter(tmp_path, "prompts/tools_v4_intent.json", lambda r: httpx.Response(500))
    r = await a.call(SCOPE, GoalState(), "store_intent", intent)
    assert r["error"]["type"] == "invalid_arguments"
    box = FakeOutbox()
    st = GoalState()
    r = await adapter(tmp_path, "prompts/tools_v4_intent.json", lambda r: httpx.Response(500), box).call(
        SCOPE, st, "store_intent", intent)
    assert r["stored"] and st.intent_id == 1 and box.rows == [("g_0003", 101, intent)]
    bad = await a.call(SCOPE, GoalState(), "store_intent", {**intent, "earliest_start": "3pm"})
    assert bad["error"]["type"] == "invalid_arguments"  # the time must be parsed into HH:MM


def worker_backend(slots, taken=()):
    calls = []

    def backend(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, dict(req.url.params), req.content))
        if req.url.path == "/appointments/101":
            return httpx.Response(200, json=APPT)
        if req.url.path == "/availability/search":
            return httpx.Response(200, json={"total_count": len(slots), "slots": slots})
        slot = json.loads(req.content)["slot_id"]
        if slot in taken:
            return httpx.Response(409, json={"error": {"type": "slot_taken", "message": "taken"}})
        return httpx.Response(200, json={"change_id": 900 + slot})
    return backend, calls


def slot(i, hhmm, tech=5):
    return {"slot_id": i, "date": "2026-11-10", "starts_at": f"2026-11-10T{hhmm}", "technician_id": tech}


ROW = {"trial_id": "tr", "goal_n": 3, "customer_id": 11, "appointment_id": 101, "regions": ["R03"],
       "intent": json.dumps({"date_from": "2026-11-09", "date_to": "2026-11-11", "earliest_start": "15:00",
                             "technician_id": 7, "customer_words": "after 3, Dana (tech 7) only"})}


async def test_worker_books_only_a_slot_meeting_every_stored_requirement(tmp_path):
    backend, calls = worker_backend([slot(1, "08:00", 7), slot(2, "15:30", 5), slot(3, "16:00", 7)])
    status, change, _ = await complete(ROW, adapter(tmp_path, WORKER_TOOLS, backend))
    assert (status, change) == ("done", 903)
    search = next(c for c in calls if c[1] == "/availability/search")[2]
    assert search["start_after"] == "15:00" and search["from"] == "2026-11-09" and search["regions"] == "R03"
    assert [c for c in calls if c[0] == "POST"][0][1] == "/appointments/101/reschedule"


async def test_worker_moves_on_after_a_conflict_and_reports_no_slot(tmp_path):
    backend, _ = worker_backend([slot(3, "16:00", 7), slot(4, "17:00", 7)], taken={3})
    assert (await complete(ROW, adapter(tmp_path, WORKER_TOOLS, backend)))[:2] == ("done", 904)
    backend, calls = worker_backend([slot(1, "08:00", 7), slot(2, "15:30", 5)])
    status, change, detail = await complete(ROW, adapter(tmp_path, WORKER_TOOLS, backend))
    assert (status, change) == ("no_slot", None) and "none met" in detail
    assert not any(c[0] == "POST" for c in calls)


def test_oracle_judges_a_handed_off_goal_by_the_database():
    afternoon = {"task_id": "T", "expected": "reschedule", "service": "hvac_repair", "d1": "2026-11-09",
                 "d2": "2026-11-11", "constraint": {"kind": "start_hour", "from_h": 15, "to_h": 18}}
    moved = dict(changes=1, other_customer_changes=0, status="scheduled", slot_id=4, slot_date=dt.date(2026, 11, 10),
                 service="hvac_repair", has_skill=True, covers_region=True, slot_occupants=1, last_change_to_slot=4)
    assert classify(afternoon, Facts(**moved, starts_at=dt.datetime(2026, 11, 10, 15, 30)), "handed_off") == "correct"
    # a 200 OK booking at 08:00 against "15:00 or later" is a hard failure, however it was made
    assert classify(afternoon, Facts(**moved, starts_at=dt.datetime(2026, 11, 10, 8)), "handed_off") == "incorrect"
    untouched = {**moved, "changes": 0}
    assert classify(afternoon, Facts(**untouched), "handed_off") == "failed"
