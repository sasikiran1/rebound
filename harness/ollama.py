"""Local Ollama backend for the live agent (no paid API). Duck-types `client.responses.create` for agent.py.

agent.py builds a Responses-style call (instructions, input items, tools). This module converts it to one Ollama
/api/chat request, sends it through the Windows relay (scripts/ollama_relay_win.py) and converts the reply back to a
Responses-shaped object. It never retries. Every raw request and response is written to `raw_dir` so tool calls and
model text can be checked against the trial's event log.

Only full_history continuation is supported: Ollama keeps no server-side conversation.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

from harness.provider import ProviderFailed

WIN_PY = "/mnt/c/Users/kiran/AppData/Local/Programs/Python/Python312/python.exe"
RELAY = r"\\wsl.localhost\Ubuntu\home\kiran\rebound\scripts\ollama_relay_win.py"


class Relay:
    """One long-lived Windows Python process; requests multiplexed by id over its stdin/stdout."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.ids = itertools.count(1)
        self.reader: asyncio.Task | None = None
        # Concurrent first calls must start ONE process. Without this lock, two goals calling at once each started
        # a relay and the reader followed only the second, so the first goal's reply was never delivered
        # (found in the semantic_v1 races, 2026-10-28; those runs are labeled invalid).
        self.start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self.start_lock:
            if self.proc is not None:
                return
            proc = await asyncio.create_subprocess_exec(
                WIN_PY, "-u", RELAY, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=64 * 2**20)
            self.proc = proc
            self.reader = asyncio.create_task(self._read(proc))

    async def _read(self, proc) -> None:
        while line := await proc.stdout.readline():
            msg = json.loads(line)
            fut = self.pending.pop(msg["id"], None)
            if fut and not fut.done():
                fut.set_result(msg)
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ProviderFailed("relay_exit", "Ollama relay process exited", 0, 0.0))

    async def post(self, path: str, payload: dict) -> dict:
        if self.proc is None:
            await self.start()
        rid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        self.proc.stdin.write(json.dumps({"id": rid, "path": path, "payload": payload}).encode() + b"\n")
        await self.proc.stdin.drain()
        try:
            return await fut
        finally:
            self.pending.pop(rid, None)

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
        if self.reader:
            self.reader.cancel()


def to_messages(instructions: str, items: list[dict]) -> list[dict]:
    """Responses input items -> Ollama chat messages. Consecutive function calls join the preceding assistant turn."""
    msgs = [{"role": "system", "content": instructions}]
    names: dict[str, str] = {}
    for it in items:
        if it.get("type") == "function_call":
            names[it["call_id"]] = it["name"]
            call = {"function": {"name": it["name"], "arguments": json.loads(it["arguments"] or "{}")}}
            if msgs[-1]["role"] == "assistant":
                msgs[-1].setdefault("tool_calls", []).append(call)
            else:
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
        elif it.get("type") == "function_call_output":
            msgs.append({"role": "tool", "tool_name": names.get(it["call_id"], ""), "content": it["output"]})
        else:
            msgs.append({"role": it["role"], "content": it["content"]})
    return msgs


def to_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}} for t in tools]


class OllamaResponses:
    def __init__(self, relay: Relay, options: dict, think: bool, keep_alive: str, raw_dir: Path | None):
        self.relay, self.options, self.think, self.keep_alive = relay, options, think, keep_alive
        self.raw_dir = raw_dir
        self.n = itertools.count(1)
        if raw_dir:
            raw_dir.mkdir(parents=True, exist_ok=True)

    async def create(self, *, model, instructions, input, tools, max_output_tokens, **_ignored):
        k = next(self.n)
        payload = {"model": model, "messages": to_messages(instructions, input), "tools": to_tools(tools),
                   "stream": False, "keep_alive": self.keep_alive,
                   "options": {**self.options, "num_predict": max_output_tokens}}
        if self.think is not None:  # None: model without a thinking mode (qwen2.5); the field is then omitted
            payload["think"] = self.think
        t0 = time.perf_counter()
        res = await self.relay.post("/api/chat", payload)
        wall_ms = (time.perf_counter() - t0) * 1000
        if self.raw_dir:
            (self.raw_dir / f"call_{k:05d}.json").write_text(json.dumps(
                {"request": payload, "response": res, "runner_wall_ms": round(wall_ms, 1)}, ensure_ascii=False))
        if not res.get("ok"):
            raise ProviderFailed(res.get("status") or "ollama_error", str(res.get("error"))[:300], 0, 0.0)
        body = res["body"]
        msg = body.get("message") or {}
        output = []
        if msg.get("content"):
            output.append(NS(type="message", content=[NS(type="output_text", text=msg["content"])]))
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c["function"]
            args = fn.get("arguments")
            output.append(NS(type="function_call", call_id=f"call_{k}_{i}", name=fn["name"],
                             arguments=args if isinstance(args, str) else json.dumps(args)))
        prompt_n, out_n = body.get("prompt_eval_count") or 0, body.get("eval_count") or 0
        status = "incomplete" if body.get("done_reason") == "length" else "completed"
        return NS(id=f"ollama_{k}", status=status, output=output,
                  done_reason=body.get("done_reason"), server_s=res.get("elapsed_s"),
                  context_used=prompt_n + out_n, num_ctx=self.options.get("num_ctx"),
                  usage=NS(input_tokens=prompt_n, output_tokens=out_n,
                           input_tokens_details=NS(cached_tokens=0), output_tokens_details=NS(reasoning_tokens=0)))


class OllamaClient:
    """`client.responses.create(...)` like openai.AsyncOpenAI, backed by local Ollama."""

    def __init__(self, options: dict, think: bool = False, keep_alive: str = "60m", raw_dir: Path | None = None):
        self.relay = Relay()
        self.responses = OllamaResponses(self.relay, options, think, keep_alive, raw_dir)

    async def close(self) -> None:
        await self.relay.close()
