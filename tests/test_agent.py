import json
from types import SimpleNamespace as NS

import httpx

from harness.adapter import ToolAdapter, load_tools
from harness.agent import AgentConfig, run_goal
from harness.events import EventLog
from harness.provider import Provider

TASK = {"task_id": "A0001", "customer_id": 11, "appointment_id": 101, "allowed_regions": ["R03"],
        "goal_text": "Customer 11 wants ...", "d1": "2026-11-05", "d2": "2026-11-05"}


def usage(i=1000, cached=0, o=50):
    return NS(input_tokens=i, output_tokens=o, input_tokens_details=NS(cached_tokens=cached),
              output_tokens_details=NS(reasoning_tokens=10))


def fn_call(name, args, n):
    return NS(id=f"resp_{n}", status="completed", usage=usage(),
              output=[NS(type="function_call", name=name, arguments=json.dumps(args), call_id=f"call_{n}")])


def message(text, n):
    return NS(id=f"resp_{n}", status="completed", usage=usage(),
              output=[NS(type="message", content=[NS(type="output_text", text=text)])])


class ScriptedModel:
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.responses = self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.script.pop(0)


def backend(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/customers/11/appointments":
        return httpx.Response(200, json={"region": "R03", "appointments": [{"appointment_id": 101}]})
    if req.url.path.endswith("/reschedule"):
        return httpx.Response(200, json={"change_id": 9})
    return httpx.Response(200, json={"appointment_id": 101})


async def run(tmp_path, script, cfg=None):
    model = ScriptedModel(script)
    events = EventLog(tmp_path, trial_id="tr_t")
    tools = load_tools()
    adapter = ToolAdapter("http://fd.test", httpx.AsyncClient(transport=httpx.MockTransport(backend)), tools, events)
    out = await run_goal(TASK, 1, "tr_t", Provider(model), adapter, events, tools, cfg or AgentConfig())
    events.close()
    return out, model


async def test_happy_path_chains_previous_response_id(tmp_path):
    out, model = await run(tmp_path, [
        fn_call("get_customer_appointments", {"customer_id": 11}, 1),
        fn_call("reschedule_appointment", {"appointment_id": 101, "slot_id": 5}, 2),
        fn_call("get_appointment", {"appointment_id": 101}, 3),
        message("Done.", 4),
    ])
    assert out.end_reason == "rescheduled" and out.change_id == 9
    assert out.model_calls == 4 and out.tool_calls == 3
    assert [r["previous_response_id"] for r in model.requests] == [None, "resp_1", "resp_2", "resp_3"]
    req = model.requests[1]
    assert req["store"] is True and req["parallel_tool_calls"] is False and req["reasoning"] == {"effort": "low"}
    assert req["input"][0]["type"] == "function_call_output" and req["input"][0]["call_id"] == "call_1"
    assert out.input_tokens == 4000


async def test_full_history_resends_conversation_without_stored_responses(tmp_path):
    out, model = await run(tmp_path, [
        fn_call("get_customer_appointments", {"customer_id": 11}, 1),
        fn_call("get_appointment", {"appointment_id": 101}, 2),
        message("Done.", 3),
    ], AgentConfig(continuation="full_history"))
    assert out.model_calls == 3
    assert all("previous_response_id" not in r and r["store"] is False for r in model.requests)
    sizes = [len(r["input"]) for r in model.requests]
    assert sizes == [1, 3, 5]  # user; + call 1 and its output; + call 2 and its output
    second = model.requests[1]["input"]
    assert second[1] == {"type": "function_call", "call_id": "call_1", "name": "get_customer_appointments",
                         "arguments": '{"customer_id": 11}'}
    assert second[2]["type"] == "function_call_output" and second[2]["call_id"] == "call_1"


async def test_defer_ends_goal_without_another_model_call(tmp_path):
    out, model = await run(tmp_path, [fn_call("defer_goal", {"reason": "no slot"}, 1)])
    assert out.end_reason == "deferred" and out.deferred_reason == "no slot" and len(model.requests) == 1


async def test_tool_call_limit(tmp_path):
    script = [fn_call("get_appointment", {"appointment_id": 101}, n) for n in range(1, 10)]
    out, _ = await run(tmp_path, script, AgentConfig(max_tool_calls=3))
    assert out.end_reason == "limit_reached" and out.limit == "tool_calls" and out.tool_calls == 3


async def test_model_call_limit(tmp_path):
    script = [fn_call("get_appointment", {"appointment_id": 101}, n) for n in range(1, 10)]
    out, _ = await run(tmp_path, script, AgentConfig(max_model_calls=2))
    assert out.end_reason == "limit_reached" and out.limit == "model_calls" and out.model_calls == 2


async def test_final_message_without_write_is_gave_up(tmp_path):
    out, _ = await run(tmp_path, [message("I could not do it.", 1)])
    assert out.end_reason == "gave_up"


async def test_provider_failure_ends_goal(tmp_path):
    from tests.test_provider import status_error

    class Failing(ScriptedModel):
        async def create(self, **kwargs):
            raise status_error(400, body={"error": {"message": "bad model"}})

    events = EventLog(tmp_path, trial_id="tr_t")
    tools = load_tools()
    adapter = ToolAdapter("http://fd.test", httpx.AsyncClient(transport=httpx.MockTransport(backend)), tools, events)
    out = await run_goal(TASK, 1, "tr_t", Provider(Failing([])), adapter, events, tools, AgentConfig())
    assert out.end_reason == "provider_failed" and "400" in out.provider_error
