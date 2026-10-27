import asyncio
import json

from fielddesk.db import RequestStats, current_stats
from fielddesk.gate import Gate, GateMiddleware


async def test_admits_up_to_tokens_then_queues_fifo():
    gate = Gate(tokens=2, queue_max=4, queue_wait_s=1.0)
    assert await gate.acquire() == ("admitted", None)
    assert await gate.acquire() == ("admitted", None)
    order = []

    async def waiter(i):
        decision, _ = await gate.acquire()
        order.append((i, decision))

    tasks = [asyncio.create_task(waiter(i)) for i in range(3)]
    await asyncio.sleep(0)
    assert gate.queue_len == 3
    gate.release()
    gate.release()
    await asyncio.sleep(0)
    assert order == [(0, "queued"), (1, "queued")]
    gate.release()
    await asyncio.gather(*tasks)
    assert order[-1] == (2, "queued")
    assert gate.free == 0 and gate.queue_len == 0


async def test_rejects_when_queue_full():
    gate = Gate(tokens=1, queue_max=1, queue_wait_s=1.0)
    await gate.acquire()
    t = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    assert await gate.acquire() == ("rejected", "queue_full")
    gate.release()
    assert await t == ("queued", None)
    assert gate.rejected == 1


async def test_rejects_after_queue_wait_and_frees_queue_slot():
    gate = Gate(tokens=1, queue_max=1, queue_wait_s=0.05)
    await gate.acquire()
    assert await gate.acquire() == ("rejected", "queue_timeout")
    assert gate.queue_len == 0
    gate.release()
    assert gate.free == 1  # no token leaked to the timed-out waiter


async def test_cancelled_waiter_does_not_leak_a_token():
    gate = Gate(tokens=1, queue_max=2, queue_wait_s=5.0)
    await gate.acquire()
    t = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    t.cancel()
    await asyncio.sleep(0)
    gate.release()
    assert gate.free == 1 and gate.queue_len == 0


async def run_request(mw):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    stats = RequestStats()
    token = current_stats.set(stats)
    try:
        await mw({"type": "http", "path": "/availability", "headers": [(b"x-rb-goal", b"g_0001")]}, receive, send)
    finally:
        current_stats.reset(token)
    return sent, stats


async def test_middleware_rejects_with_503_retry_after_and_no_handler_call():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    gate = Gate(tokens=1, queue_max=0, queue_wait_s=0.1)
    await gate.acquire()  # hold the only token
    sent, stats = await run_request(GateMiddleware(app, gate, retry_after_s=2))
    assert calls == []
    assert sent[0]["status"] == 503
    assert (b"retry-after", b"2") in sent[0]["headers"]
    assert json.loads(sent[1]["body"])["error"]["type"] == "overloaded"
    assert stats.gate_decision == "rejected" and stats.reject_reason == "queue_full"

    gate.release()
    sent, stats = await run_request(GateMiddleware(app, gate, retry_after_s=2))
    assert sent[0]["status"] == 200 and stats.gate_decision == "admitted"
    assert gate.free == 1  # released after the handler


async def test_gate_off_passes_everything_through():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    sent, stats = await run_request(GateMiddleware(app, None))
    assert sent[0]["status"] == 200 and stats.gate_decision == "admitted"
