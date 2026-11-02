"""Agent policy file, refusal probe middleware, wait tool, and the refusal-gap measurement."""

import json
import sys

import httpx
from fastapi import FastAPI
from fielddesk.policy import POLICY_PATH, PolicyMiddleware, policy

from harness.adapter import GoalScope, GoalState, ToolAdapter, load_tools
from harness.events import EventLog

sys.path.insert(0, "scripts")
from policy_probe import refusal_gaps, summarize  # noqa: E402


def app_with(policy_on: bool, refuse_first: int) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def ok():
        return {"ok": True}

    app.add_middleware(PolicyMiddleware, policy_on=policy_on, refuse_first=refuse_first, retry_after_s=2)
    return app


async def get(app, path, goal=None):
    headers = {"X-RB-Goal": goal} if goal else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fd") as c:
        return await c.get(path, headers=headers)


async def test_off_by_default_changes_nothing():
    app = app_with(False, 0)
    assert (await get(app, "/healthz", "g_0001")).status_code == 200
    assert (await get(app, POLICY_PATH)).status_code == 404


async def test_refuses_the_first_n_requests_of_each_goal():
    app = app_with(False, 2)
    statuses = [(await get(app, "/healthz", g)).status_code for g in ("g_1", "g_1", "g_2", "g_1", "g_2", "g_2")]
    assert statuses == [503, 503, 503, 200, 503, 200]
    r = await get(app, "/healthz", "g_3")
    assert r.headers["retry-after"] == "2" and r.json()["error"]["message"] == "server busy (probe), retry later"
    assert (await get(app, "/healthz")).status_code == 200  # no goal header: never refused


async def test_policy_on_serves_the_file_and_states_it_in_refusals():
    app = app_with(True, 1)
    r = await get(app, POLICY_PATH)
    assert r.status_code == 200 and r.json() == policy(2)
    msg = (await get(app, "/healthz", "g_1")).json()["error"]["message"]
    assert "wait tool" in msg and "2 seconds" in msg and POLICY_PATH in msg


async def test_wait_tool_sleeps_and_sends_nothing(tmp_path):
    calls, slept = [], []

    def backend(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={})

    async def sleep(s):
        slept.append(s)

    a = ToolAdapter("http://fd.test", httpx.AsyncClient(transport=httpx.MockTransport(backend)),
                    load_tools("prompts/tools_v1_wait.json"), EventLog(tmp_path, trial_id="t"), sleep=sleep)
    st = GoalState()
    r = await a.call(GoalScope("t", 1, 11, 101, ["R03"]), st, "wait", {"seconds": 2})
    assert r == {"waited_s": 2.0} and slept == [2.0] and calls == [] and (st.waits, st.wait_s) == (1, 2.0)
    r = await a.call(GoalScope("t", 1, 11, 101, ["R03"]), st, "wait", {"seconds": 99})
    assert r["error"]["type"] == "invalid_arguments" and st.waits == 1


def test_refusal_gaps_and_summary(tmp_path):
    def attempt(goal, end_s, latency_ms, status, ra):
        return {"event_type": "http.attempt", "goal_id": goal, "ts_mono_ns": int(end_s * 1e9),
                "latency_ms": latency_ms, "http_status": status, "retry_after_s": ra}

    log = tmp_path / "client.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in [
        attempt("g_1", 10.0, 5, 503, 2.0), attempt("g_1", 11.0, 10, 503, 2.0),  # next sent 0.99 s later: early
        attempt("g_1", 14.0, 100, 200, None),  # sent 2.9 s after the refusal: on time
        attempt("g_2", 20.0, 5, 503, 2.0),  # never followed
        {"event_type": "tool.op", "goal_id": "g_1"},
    ]))
    gaps = refusal_gaps(log)
    assert gaps["g_1"] == [{"retry_after_s": 2.0, "gap_s": 0.99}, {"retry_after_s": 2.0, "gap_s": 2.9}]
    assert gaps["g_2"] == [{"retry_after_s": 2.0, "gap_s": None}]
    rows = [{"verdict": "correct", "end_reason": "rescheduled", "refusals": gaps["g_1"], "waits": 1,
             "model_s": [0.5, 1.0], "cost_usd": 0.01},
            {"verdict": "wrongly_deferred", "end_reason": "deferred", "refusals": gaps["g_2"], "waits": 0,
             "model_s": [0.7], "cost_usd": 0.02}]
    s = summarize(rows)
    assert (s["refusals"], s["refusals_followed_by_a_request"], s["early_requests"], s["early_share"]) == (3, 2, 1, 0.5)
    assert s["goals_calling_wait"] == 1 and s["model_step_s_median"] == 0.7 and s["cost_usd"] == 0.03
