import asyncio
import json

import httpx
import pytest

from harness.adapter import GoalScope, GoalState, ToolAdapter, load_tools, truncate
from harness.events import EventLog

BASE = "http://fielddesk.test"


def make_adapter(tmp_path, handler, timeout_s=3.0):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = EventLog(tmp_path, trial_id="tr_test")
    return ToolAdapter(BASE, client, load_tools(), events, timeout_s=timeout_s), events


def scope():
    return GoalScope(trial_id="tr_test", goal_n=7, customer_id=11, appointment_id=101, allowed_regions=["R03"])


def read_events(tmp_path):
    return [json.loads(line) for line in (tmp_path / "client.jsonl").read_text().splitlines()]


@pytest.mark.parametrize(
    "name,args,fragment",
    [
        ("get_customer_appointments", {"customer_id": "11"}, "schema"),
        ("get_customer_appointments", {"customer_id": 11, "extra": 1}, "schema"),
        ("check_day_availability", {"region": "R03", "date": "next tuesday", "service": "x"}, "schema"),
        ("get_customer_appointments", {"customer_id": 12}, "out of scope"),
        ("get_appointment", {"appointment_id": 999}, "out of scope"),
        ("check_day_availability", {"region": "R04", "date": "2026-11-05", "service": "x"}, "out of scope"),
        ("reschedule_appointment", {"appointment_id": 102, "slot_id": 5}, "out of scope"),
        ("drop_tables", {}, "unknown tool"),
    ],
)
async def test_invalid_arguments_never_reach_http(tmp_path, name, args, fragment):
    calls = []
    adapter, _ = make_adapter(tmp_path, lambda req: calls.append(req) or httpx.Response(200, json={}))
    state = GoalState()
    out = await adapter.call(scope(), state, name, json.dumps(args))
    assert out["error"]["type"] == "invalid_arguments"
    assert fragment in out["error"]["message"]
    assert calls == []
    assert state.invalid_arguments == 1


async def test_malformed_json_is_invalid_arguments(tmp_path):
    adapter, _ = make_adapter(tmp_path, lambda req: httpx.Response(200, json={}))
    out = await adapter.call(scope(), GoalState(), "get_appointment", "{not json")
    assert out["error"]["type"] == "invalid_arguments"


async def test_write_sends_derived_key_customer_and_identity_headers(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"change_id": 1, "appointment_id": 101})

    adapter, _ = make_adapter(tmp_path, handler)
    state = GoalState()
    out = await adapter.call(
        scope(), state, "reschedule_appointment", '{"appointment_id": 101, "slot_id": 5}', "mc_0007_01"
    )
    assert out["change_id"] == 1 and state.write_applied
    req = seen[0]
    assert str(req.url) == f"{BASE}/appointments/101/reschedule"
    assert req.headers["Idempotency-Key"] == "tr_test:g_0007:reschedule"
    assert json.loads(req.content) == {"slot_id": 5, "customer_id": 11}
    assert req.headers["X-RB-Goal"] == "g_0007"
    assert req.headers["X-RB-Op"] == "op_0007_01"
    assert req.headers["X-RB-Attempt"] == "at_0007_01_1"

    # A second write after an applied one is refused without an HTTP call.
    out = await adapter.call(scope(), state, "reschedule_appointment", '{"appointment_id": 101, "slot_id": 6}')
    assert out["error"]["type"] == "invalid_arguments"
    assert len(seen) == 1


async def test_write_allowed_again_after_conflict(tmp_path):
    responses = [httpx.Response(409, json={"error": {"type": "slot_taken", "message": "taken"}}),
                 httpx.Response(200, json={"change_id": 2})]
    adapter, _ = make_adapter(tmp_path, lambda req: responses.pop(0))
    state = GoalState()
    first = await adapter.call(scope(), state, "reschedule_appointment", '{"appointment_id": 101, "slot_id": 5}')
    assert first == {"error": {"type": "http_409", "message": "taken"}}
    second = await adapter.call(scope(), state, "reschedule_appointment", '{"appointment_id": 101, "slot_id": 6}')
    assert second["change_id"] == 2


async def test_timeout_is_one_attempt_no_retry(tmp_path):
    calls = []

    async def slow(req):
        calls.append(req)
        await asyncio.sleep(1.0)
        return httpx.Response(200, json={})

    adapter, _ = make_adapter(tmp_path, slow, timeout_s=0.05)
    out = await adapter.call(scope(), GoalState(), "get_appointment", '{"appointment_id": 101}')
    assert out["error"]["type"] == "timeout"
    assert len(calls) == 1
    attempts = [e for e in read_events(tmp_path) if e["event_type"] == "http.attempt"]
    assert len(attempts) == 1 and attempts[0]["attempt_n"] == 1 and attempts[0]["retry_layer"] == "none"


async def test_503_carries_retry_after(tmp_path):
    adapter, _ = make_adapter(
        tmp_path, lambda req: httpx.Response(503, headers={"Retry-After": "2"}, json={"error": {"message": "busy"}})
    )
    out = await adapter.call(scope(), GoalState(), "get_appointment", '{"appointment_id": 101}')
    assert out == {"error": {"type": "http_503", "message": "busy", "retry_after_s": 2.0}}


async def test_listing_makes_customer_appointments_readable(tmp_path):
    def handler(req):
        if req.url.path.startswith("/customers"):
            return httpx.Response(200, json={"appointments": [{"appointment_id": 102}]})
        return httpx.Response(200, json={"appointment_id": 102})

    adapter, _ = make_adapter(tmp_path, handler)
    state = GoalState()
    assert "error" in await adapter.call(scope(), state, "get_appointment", '{"appointment_id": 102}')
    await adapter.call(scope(), state, "get_customer_appointments", '{"customer_id": 11}')
    assert "error" not in await adapter.call(scope(), state, "get_appointment", '{"appointment_id": 102}')


async def test_defer_is_harness_only(tmp_path):
    adapter, _ = make_adapter(tmp_path, lambda req: pytest.fail("no HTTP for defer"))
    state = GoalState()
    assert await adapter.call(scope(), state, "defer_goal", '{"reason": "no slot"}') == {"deferred": True}
    assert state.deferred_reason == "no slot"


async def test_events_carry_ids(tmp_path):
    adapter, _ = make_adapter(tmp_path, lambda req: httpx.Response(200, json={"appointment_id": 101}))
    await adapter.call(scope(), GoalState(), "get_appointment", '{"appointment_id": 101}', "mc_0007_01")
    for e in read_events(tmp_path):
        assert e["trial_id"] == "tr_test" and e["goal_id"] == "g_0007"
        assert e["logical_op_id"] == "op_0007_01" and e["model_call_id"] == "mc_0007_01"


def test_truncate_keeps_under_limit_with_counts():
    result = {"region": "R03", "slots": [{"slot_id": i, "starts_at": "2026-11-05T08:00"} for i in range(500)]}
    out = truncate(result, 4096)
    assert len(json.dumps(out)) <= 4096
    assert out["total_count"] == 500 and out["truncated"] and out["returned_count"] == len(out["slots"]) > 0
    small = {"slots": [1, 2]}
    assert truncate(small, 4096) is small
