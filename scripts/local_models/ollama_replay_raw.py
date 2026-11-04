"""Diagnostic: resend one recorded Ollama request, optionally without its tool list, and print the model's raw text.

Usage: python scripts/local_models/ollama_replay_raw.py <call_NNNNN.json> [--no-tools]
With --no-tools Ollama's tool-call parser is not engaged, so text it would otherwise consume becomes visible.
This is a diagnostic of the runtime integration, not an agent run: no tool is executed.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from harness.ollama import Relay  # noqa: E402


async def main() -> None:
    rec = json.loads(Path(sys.argv[1]).read_text())  # noqa: ASYNC240 - one small file before any request
    payload = rec["request"]
    if "--no-tools" in sys.argv:
        payload = {k: v for k, v in payload.items() if k != "tools"}
    relay = Relay()
    res = await relay.post("/api/chat", payload)
    await relay.close()
    b = res.get("body") or {}
    m = b.get("message") or {}
    print("ok", res.get("ok"), "done", b.get("done_reason"), "eval", b.get("eval_count"))
    print("tool_calls", m.get("tool_calls"))
    print("text", repr(m.get("content"))[:1500])


asyncio.run(main())
