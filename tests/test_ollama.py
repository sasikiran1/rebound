import asyncio
import json

import pytest

import harness.ollama as ollama
from harness.ollama import OllamaResponses, Relay, to_messages, to_tools
from harness.provider import ProviderFailed


def test_to_messages_joins_calls_to_the_assistant_turn_and_names_tool_outputs():
    items = [{"role": "user", "content": "Please handle the task."},
             {"role": "assistant", "content": "Looking it up."},
             {"type": "function_call", "call_id": "c1", "name": "get_appointment",
              "arguments": '{"appointment_id": 7}'},
             {"type": "function_call", "call_id": "c2", "name": "list_technicians", "arguments": ""},
             {"type": "function_call_output", "call_id": "c1", "output": '{"ok": 1}'},
             {"type": "function_call_output", "call_id": "c2", "output": '{"ok": 2}'}]
    msgs = to_messages("system prompt", items)
    assert msgs[0] == {"role": "system", "content": "system prompt"}
    assert msgs[2]["role"] == "assistant" and [c["function"]["name"] for c in msgs[2]["tool_calls"]] == [
        "get_appointment", "list_technicians"]
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == {"appointment_id": 7}
    assert msgs[2]["tool_calls"][1]["function"]["arguments"] == {}
    assert [m["tool_name"] for m in msgs[3:]] == ["get_appointment", "list_technicians"]


def test_function_call_without_preceding_text_gets_its_own_assistant_turn():
    msgs = to_messages("s", [{"role": "user", "content": "go"},
                             {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"}])
    call = {"function": {"name": "x", "arguments": {}}}
    assert msgs[-1] == {"role": "assistant", "content": "", "tool_calls": [call]}


def test_to_tools_wraps_responses_tools_for_ollama():
    t = to_tools([{"type": "function", "name": "a", "description": "d", "parameters": {"type": "object"}}])
    assert t == [{"type": "function", "function": {"name": "a", "description": "d", "parameters": {"type": "object"}}}]


class FakeRelay:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    async def post(self, path, payload):
        self.sent.append((path, payload))
        return self.reply


def body(**msg):
    return {"ok": True, "elapsed_s": 0.5, "body": {"message": msg, "done_reason": "stop", "prompt_eval_count": 100,
                                                   "eval_count": 20}}


async def create(relay, think=False, raw_dir=None):
    r = OllamaResponses(relay, {"temperature": 0, "num_ctx": 8192}, think, "60m", raw_dir)
    return await r.create(model="m", instructions="s", input=[{"role": "user", "content": "go"}], tools=[],
                          max_output_tokens=256)


async def test_reply_becomes_a_responses_shaped_object(tmp_path):
    relay = FakeRelay(body(content="ok", tool_calls=[{"function": {"name": "get_appointment",
                                                                   "arguments": {"appointment_id": 7}}}]))
    resp = await create(relay, raw_dir=tmp_path)
    assert [o.type for o in resp.output] == ["message", "function_call"]
    assert json.loads(resp.output[1].arguments) == {"appointment_id": 7}
    assert resp.usage.input_tokens == 100 and resp.usage.output_tokens == 20 and resp.status == "completed"
    assert resp.context_used == 120 and resp.num_ctx == 8192
    sent = relay.sent[0][1]
    assert sent["options"]["num_predict"] == 256 and sent["think"] is False
    rec = json.loads((tmp_path / "call_00001.json").read_text())
    assert rec["request"]["model"] == "m" and rec["response"]["ok"] is True


async def test_think_none_omits_the_field_and_length_is_incomplete():
    reply = body(content="cut")
    reply["body"]["done_reason"] = "length"
    relay = FakeRelay(reply)
    resp = await create(relay, think=None)
    assert "think" not in relay.sent[0][1] and resp.status == "incomplete" and resp.done_reason == "length"


async def test_tokens_without_text_or_tool_call_are_flagged_as_dropped():
    # Mistral Nemo: 73 tokens generated, empty message (the parser consumed a call to a nonexistent tool)
    reply = body(content="")
    reply["body"]["eval_count"] = 73
    resp = await create(FakeRelay(reply))
    assert resp.output == [] and resp.dropped_output is True
    assert (await create(FakeRelay(body(content="done")))).dropped_output is False


async def test_ollama_error_becomes_provider_failed_without_retry():
    relay = FakeRelay({"ok": False, "status": 500, "error": "CUDA error", "elapsed_s": 1.0})
    with pytest.raises(ProviderFailed) as e:
        await create(relay)
    assert e.value.status == 500 and len(relay.sent) == 1


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.stdin = self
        self.stdout = self
        self.lines: asyncio.Queue = asyncio.Queue()

    def write(self, data):
        self.lines.put_nowait(json.dumps({"id": json.loads(data)["id"], "ok": True}).encode() + b"\n")

    async def drain(self):
        await asyncio.sleep(0)

    async def readline(self):
        return await self.lines.get()


async def test_concurrent_first_calls_start_one_relay_and_all_get_replies(monkeypatch):
    started = []

    async def fake_exec(*args, **kwargs):
        await asyncio.sleep(0.01)  # widen the race window the start lock closes
        p = FakeProc()
        started.append(p)
        return p

    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", fake_exec)
    relay = Relay()
    replies = await asyncio.wait_for(asyncio.gather(*(relay.post("/api/tags", None) for _ in range(6))), 2)
    assert len(started) == 1 and all(r["ok"] for r in replies)

    # Without the lock (the semantic_v1 defect) the same calls start several relays.
    import contextlib
    started.clear()
    unlocked = Relay()
    unlocked.start_lock = contextlib.nullcontext()
    tasks = [asyncio.create_task(unlocked.post("/api/tags", None)) for _ in range(6)]
    await asyncio.sleep(0.1)
    assert len(started) > 1
    for t in tasks:
        t.cancel()
