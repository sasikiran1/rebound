"""Stateless continuation must carry every output item verbatim, reasoning included (audit finding 1)."""

import json
from types import SimpleNamespace as NS

import pytest

from harness.agent import AgentConfig, check_history, item_to_input
from harness.events import EventLog
from tests.test_agent import TASK, ScriptedModel, backend, usage


def reasoning(n):
    return NS(type="reasoning", id=f"rs_{n}", summary=[], encrypted_content=f"enc_{n}", status=None)


def fn_call_with_reasoning(name, args, n):
    return NS(id=f"resp_{n}", status="completed", usage=usage(),
              output=[reasoning(n), NS(type="function_call", id=f"fc_{n}", name=name, arguments=json.dumps(args),
                                       call_id=f"call_{n}", status="completed")])


def final(text, n):
    return NS(id=f"resp_{n}", status="completed", usage=usage(),
              output=[reasoning(n), NS(type="message", id=f"msg_{n}", role="assistant", status="completed",
                                       content=[NS(type="output_text", text=text, annotations=[])])])


async def run_stateless(tmp_path, script):
    import httpx

    from harness.adapter import ToolAdapter, load_tools
    from harness.agent import run_goal
    from harness.provider import Provider

    model = ScriptedModel(script)
    events = EventLog(tmp_path, trial_id="tr_t")
    tools = load_tools()
    adapter = ToolAdapter("http://fd.test", httpx.AsyncClient(transport=httpx.MockTransport(backend)), tools, events)
    out = await run_goal(TASK, 1, "tr_t", Provider(model), adapter, events, tools,
                         AgentConfig(continuation="stateless_reasoning"))
    events.close()
    return out, model


async def test_stateless_reasoning_replays_reasoning_and_calls_in_order(tmp_path):
    out, model = await run_stateless(tmp_path, [
        fn_call_with_reasoning("get_customer_appointments", {"customer_id": 11}, 1),
        fn_call_with_reasoning("get_appointment", {"appointment_id": 101}, 2),
        final("Done.", 3),
    ])
    assert out.model_calls == 3
    for r in model.requests:
        assert r["store"] is False and r["include"] == ["reasoning.encrypted_content"]
        assert "previous_response_id" not in r
    second = model.requests[1]["input"]
    assert [i.get("type", i.get("role")) for i in second] == ["user", "reasoning", "function_call",
                                                              "function_call_output"]
    assert second[1] == {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc_1"}
    assert second[2]["call_id"] == "call_1" and second[2]["id"] == "fc_1" and second[3]["call_id"] == "call_1"
    third = model.requests[2]["input"]
    assert [i.get("type", i.get("role")) for i in third] == ["user", "reasoning", "function_call",
                                                             "function_call_output", "reasoning", "function_call",
                                                             "function_call_output"]
    assert third[:4] == second  # earlier items are resent unchanged
    calls = [json.loads(line) for line in (tmp_path / "model_calls.jsonl").read_text().splitlines()]
    assert [c["input_reasoning_items"] for c in calls] == [0, 1, 2]
    assert all("enc_" not in line for line in (tmp_path / "client.jsonl").read_text().splitlines())


def test_item_to_input_keeps_typed_fields_and_drops_unset():
    item = NS(type="message", id="msg_1", role="assistant", status="completed", phase=None,
              content=[NS(type="output_text", text="hi", annotations=[])])
    assert item_to_input(item) == {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                                   "content": [{"type": "output_text", "text": "hi", "annotations": []}]}


@pytest.mark.parametrize("items, problem", [
    ([{"type": "function_call", "call_id": "a"}], "has no output"),
    ([{"type": "function_call_output", "call_id": "a"}], "unknown"),
    ([{"type": "function_call", "call_id": "a"}, {"type": "function_call_output", "call_id": "a"},
      {"type": "function_call_output", "call_id": "a"}], "unknown or already answered"),
])
def test_check_history_rejects_unpaired_tool_items(items, problem):
    assert any(problem in p for p in check_history(items))


def test_check_history_accepts_paired_items():
    items = [{"role": "user", "content": "x"}, {"type": "reasoning", "id": "r"},
             {"type": "function_call", "call_id": "a"}, {"type": "function_call_output", "call_id": "a"}]
    assert check_history(items) == []
