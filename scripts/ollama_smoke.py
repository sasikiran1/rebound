"""Relay smoke test and throughput probe (no FieldDesk). Usage: python scripts/ollama_smoke.py <model> [parallel ...]

1. One trivial tool-calling request through harness/ollama.py, printed raw.
2. For each parallel level N: N identical short requests at once; prints per-request server latency and wall time,
   which shows how many requests Ollama runs concurrently on this GPU.
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")
from harness.adapter import load_tools  # noqa: E402
from harness.ollama import OllamaClient  # noqa: E402

OPTS = {"temperature": 0, "seed": 41, "num_ctx": 16384}


async def main() -> None:
    model = sys.argv[1]
    levels = [int(x) for x in sys.argv[2:]] or [1, 2, 4, 8]
    client = OllamaClient(OPTS)
    tools = load_tools()
    item = [{"role": "user", "content": "Please handle the task."}]
    instr = "You are a FieldDesk assistant. Task: list the appointments of customer 11358."
    t0 = time.perf_counter()
    r = await client.responses.create(model=model, instructions=instr, input=item, tools=tools, max_output_tokens=256)
    print("first call (includes load):", round(time.perf_counter() - t0, 2), "s", r.status, r.done_reason,
          [(o.type, getattr(o, "name", None), getattr(o, "arguments", None)) for o in r.output], vars(r.usage)["input_tokens"])
    for n in levels:
        t0 = time.perf_counter()
        rs = await asyncio.gather(*(client.responses.create(model=model, instructions=instr, input=item, tools=tools,
                                                            max_output_tokens=256) for _ in range(n)))
        wall = time.perf_counter() - t0
        print(f"parallel {n}: wall {wall:.2f} s; server s {[round(x.server_s, 2) for x in rs]}; "
              f"out tokens {[x.usage.output_tokens for x in rs]}")
    await client.close()


asyncio.run(main())
