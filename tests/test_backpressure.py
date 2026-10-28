"""Back-pressure compliance, the runtime Retry-After guard, exposure shares and the robust scripted baseline."""

import json

import httpx
import pandas as pd

from analysis.metrics import backpressure, exposure
from harness.adapter import GoalScope, GoalState, ToolAdapter, load_tools
from harness.events import EventLog
from harness.scripted import ScriptedConfig, run_goal

# --- analysis: Retry-After compliance ---------------------------------------------------------------------------


class FakeTD:
    def __init__(self, client):
        self.client = client

    def t(self, ns):
        return ns / 1e9


def att(goal, recv_s, status, ra=None, latency_ms=5.0, aid="a", mc=None, tool="get_appointment"):
    return {"event_type": "http.attempt", "goal_id": goal, "ts_mono_ns": int(recv_s * 1e9), "latency_ms": latency_ms,
            "http_status": status, "retry_after_s": ra, "attempt_id": aid, "model_call_id": mc, "tool": tool}


def test_backpressure_counts_early_requests_after_retry_after():
    client = [
        att("g_1", 10.0, 503, ra=2.0, aid="at_1_01_1"),
        att("g_1", 10.8, 503, ra=2.0, aid="at_1_02_1", mc="mc_1_02"),  # sent ~0.8 s later: violation
        att("g_1", 13.0, 200, aid="at_1_03_1", mc="mc_1_03"),  # sent 2.2 s after the second refusal: compliant
        att("g_2", 10.0, 503, ra=2.0, aid="at_2_01_1"),
        att("g_2", 11.99, 200, latency_ms=0.0, aid="at_2_02_1"),  # 1.99 s: inside the 50 ms tolerance
        att("g_3", 10.0, 503, ra=None),  # no Retry-After: not a signal
        att("g_3", 10.1, 200),
    ]
    r = backpressure(FakeTD(client))
    assert r["signals"] == 3 and r["followed_by_attempt"] == 3
    assert r["violations"] == 1 and r["goals_with_violation"] == 1
    w = r["witnesses"][0]
    assert w["refused_attempt_id"] == "at_1_01_1" and w["next_attempt_id"] == "at_1_02_1"
    assert w["next_model_call_id"] == "mc_1_02" and 0.7 < w["gap_s"] < 0.85


def test_exposure_shares_by_overlap():
    be = pd.DataFrame([
        {"t": 0.0, "total_ms": 10_000, "db_ms": 100.0, "goal_id": None, "actor": "background", "gate": "admitted"},
        {"t": 5.0, "total_ms": 2_000, "db_ms": 50.0, "goal_id": "g_1", "actor": "agent", "gate": "admitted"},
        {"t": 6.0, "total_ms": 0.0, "db_ms": 0.0, "goal_id": "g_1", "actor": "agent", "gate": "rejected"},
    ])
    w = exposure(be, {"fault": (4.0, 6.0)})["fault"]
    assert w["goal_inflight_s"] == 1.0 and w["background_inflight_s"] == 2.0
    assert abs(w["goal_share_inflight"] - 1 / 3) < 1e-9
    assert w["goal_db_ms"] == 25.0 and w["background_db_ms"] == 20.0
    assert w["goal_requests_started"] == 1 and w["goal_requests_rejected"] == 0  # t=6.0 is outside [4, 6)


# --- runtime Retry-After guard -----------------------------------------------------------------------------------


def make_adapter(tmp_path, handler, **kw):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    events = EventLog(tmp_path, trial_id="tr_t")
    a = ToolAdapter("http://fd.test", httpx.AsyncClient(transport=httpx.MockTransport(handler)), load_tools(), events,
                    sleep=fake_sleep, **kw)
    return a, events, waits


async def test_guard_holds_next_attempt_until_retry_after(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "2"}, json={"error": {"message": "busy"}})
        return httpx.Response(200, json={"appointment_id": 101})

    adapter, events, waits = make_adapter(tmp_path, handler, retry_after_guard=True)
    scope, state = GoalScope("tr_t", 1, 11, 101, ["R03"]), GoalState()
    first = await adapter.call(scope, state, "get_appointment", {"appointment_id": 101})
    assert first["error"]["retry_after_s"] == 2.0 and not waits
    await adapter.call(scope, state, "get_appointment", {"appointment_id": 101})
    events.close()
    assert len(waits) == 1 and 1.9 < waits[0] <= 2.0  # the model's next request waited out Retry-After
    assert state.retry_after_holds == 1 and len(calls) == 2  # nothing extra was sent
    rows = [json.loads(x) for x in (tmp_path / "client.jsonl").read_text().splitlines()]
    holds = [r["retry_after_hold_ms"] for r in rows if r["event_type"] == "http.attempt"]
    assert holds[0] is None and 1900 < holds[1] <= 2000


async def test_no_guard_by_default(tmp_path):
    def handler(_req):
        return httpx.Response(503, headers={"Retry-After": "2"}, json={"error": {"message": "busy"}})

    adapter, events, waits = make_adapter(tmp_path, handler)
    scope, state = GoalScope("tr_t", 1, 11, 101, ["R03"]), GoalState()
    for _ in range(3):
        await adapter.call(scope, state, "get_appointment", {"appointment_id": 101})
    events.close()
    assert waits == [] and state.attempts == 3


# --- robust scripted baseline -------------------------------------------------------------------------------------

STASK = {"task_id": "A0001", "customer_id": 11, "appointment_id": 101, "allowed_regions": ["R03"],
         "service": "repair", "current_date": "2026-11-01", "d1": "2026-11-05", "d2": "2026-11-05"}


def workflow(fail_listing=0, write_500_applied=False):
    state = {"listing": 0, "writes": 0, "slot": 7}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/customers/11/appointments":
            state["listing"] += 1
            if state["listing"] <= fail_listing:
                return httpx.Response(503, headers={"Retry-After": "1"}, json={"error": {"message": "busy"}})
            return httpx.Response(200, json={"region": "R03", "appointments": [
                {"appointment_id": 101, "status": "scheduled", "service": "repair", "date": "2026-11-01",
                 "slot_id": 7}]})
        if path == "/availability":
            return httpx.Response(200, json={"slots": [{"slot_id": 55}]})
        if path.endswith("/reschedule"):
            state["writes"] += 1
            state["slot"] = 55
            if write_500_applied and state["writes"] == 1:
                return httpx.Response(500, json={"error": {"message": "lost response"}})
            return httpx.Response(200, json={"change_id": 9})
        return httpx.Response(200, json={"appointment_id": 101, "slot_id": state["slot"]})

    return handler, state


async def scripted(tmp_path, handler, policy, budget=20):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    events = EventLog(tmp_path, trial_id="tr_t")
    adapter = ToolAdapter("http://fd.test", httpx.AsyncClient(transport=httpx.MockTransport(handler)), load_tools(),
                          events, sleep=fake_sleep)
    cfg = ScriptedConfig(think_time_s=0.0, policy=policy, request_budget=budget)
    out = await run_goal(STASK, 1, "tr_t", adapter, events, cfg, sleep=fake_sleep)
    events.close()
    return out


async def test_v1_abandons_after_first_read_fails_but_robust_v2_completes(tmp_path):
    h1, st1 = workflow(fail_listing=3)
    v1 = await scripted(tmp_path / "v1", h1, "v1")
    # every v1 request was refused with Retry-After, so the harness labels the goal `rejected`; nothing was written
    assert v1.end_reason == "rejected" and v1.change_id is None and st1["writes"] == 0
    h2, st = workflow(fail_listing=3)
    out = await scripted(tmp_path / "v2", h2, "robust_v2")
    assert out.end_reason == "rescheduled" and st["writes"] == 1


async def test_robust_v2_reconciles_uncertain_write_without_second_write(tmp_path):
    h, st = workflow(write_500_applied=True)
    out = await scripted(tmp_path, h, "robust_v2")
    assert out.end_reason == "rescheduled" and st["writes"] == 1  # read-back found the committed change


async def test_robust_v2_budget_counts_every_attempt(tmp_path):
    h, st = workflow(fail_listing=100)
    out = await scripted(tmp_path, h, "robust_v2", budget=5)
    assert out.attempts == 5 and st["writes"] == 0  # retries stop exactly at the budget, internal ones included
    assert out.end_reason == "rejected"  # every attempt was a refusal with Retry-After
