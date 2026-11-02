"""Compare the original agent with the hardened one on free-text tasks; the SQL oracle judges both.

Usage: python scripts/agent_v2_run.py <out_dir> --tasks data/tasks/tasks_messy_v1.jsonl --split dev|held_out|all
         --arms original,hardened --provider ollama|meta [--model NAME] [--concurrency N] [--cap-usd X]

original = prompts/agent_v1.md + tools_v1. hardened = prompts/agent_v2.md + tools_v3 (start-time filters and
record_requirements, enforced by code before any booking). No fault, gate off, no background traffic, fresh database
per arm. --provider meta spends real money: the estimate is printed first, and no new goal starts once --cap-usd is
reached. The agent sees only the customer's message; the task's structured constraint is used by the oracle alone.
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

import asyncpg

sys.path.insert(0, ".")
from harness import dbadmin, settings, sut  # noqa: E402
from harness.adapter import ToolAdapter, load_tools, new_http_client  # noqa: E402
from harness.agent import AgentConfig, run_goal  # noqa: E402
from harness.events import EventLog  # noqa: E402
from harness.manifest import load  # noqa: E402
from harness.ollama import OllamaClient  # noqa: E402
from harness.provider import (  # noqa: E402
    Provider,
    cost_usd,  # noqa: E402
    make_client,
)
from harness.single import TYPICAL_CACHED, TYPICAL_INPUT, TYPICAL_OUTPUT  # noqa: E402
from oracle.verdicts import verdict_for  # noqa: E402

ARMS = {"original": ("prompts/agent_v1.md", "prompts/tools_v1.json"),
        "hardened": ("prompts/agent_v2.md", "prompts/tools_v3.json")}


class CapturingAdapter(ToolAdapter):
    """Keeps each goal's state so the requirements the model recorded can be reported."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.states: dict = {}

    async def call(self, scope, state, name, raw_args, *a, **kw):
        self.states[scope.goal_id] = state
        return await super().call(scope, state, name, raw_args, *a, **kw)


async def run_arm(arm: str, tasks: list[dict], args, out: Path) -> list[dict]:
    prompt, tool_file = ARMS[arm]
    await dbadmin.clone_trial_db()
    m = load("manifests/frozen/eval_v1.yaml")
    await asyncio.to_thread(sut.restart_fielddesk, sut.fielddesk_env(m, False))
    folder = out / arm
    events = EventLog(folder, experiment_id="agent-v2", trial_id=f"av2_{arm}", condition="agent", gate="off",
                      config_hash=None)
    tools = load_tools(tool_file)
    if args.provider == "ollama":
        client = OllamaClient({"temperature": 0, "seed": 41, "num_ctx": 16384}, think=False,
                              raw_dir=folder / "ollama_raw")
    else:
        client = make_client()
    provider = Provider(client)
    cfg = AgentConfig(model=args.model, prompt_path=prompt, continuation="full_history", max_output_tokens=2048,
                      max_model_calls=16, max_tool_calls=14, goal_deadline_s=240)
    spent = [args.spent]
    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []
    async with new_http_client() as http:
        adapter = CapturingAdapter(settings.FIELDDESK_URL, http, tools, events)

        async def one(n: int, t: dict) -> None:
            async with sem:
                if args.cap_usd is not None and spent[0] >= args.cap_usd:
                    rows.append({"arm": arm, "task_id": t["task_id"], "verdict": "not_run_budget",
                                 "kind": t["messy_kind"], "split": t["split"]})
                    return
                seen = {k: v for k, v in t.items() if k not in ("constraint", "messy_kind", "feasible", "split")}
                o = await run_goal(seen, n, f"av2_{arm}", provider, adapter, events, tools, cfg)
                spent[0] += o.cost_usd
                conn = await asyncpg.connect(settings.oracle_dsn())
                try:
                    v = await verdict_for(conn, t, o.end_reason)
                finally:
                    await conn.close()
                st = adapter.states.get(o.goal_id)
                rows.append({"arm": arm, "task_id": t["task_id"], "kind": t["messy_kind"], "split": t["split"],
                             "verdict": v["verdict"], "end_reason": o.end_reason, "limit": o.limit,
                             "booked": str(v["facts"].get("starts_at"))[11:16] if v["facts"]["changes"] else None,
                             "constraint": t["constraint"], "recorded": st.requirements if st else None,
                             "blocks": st.requirement_blocks if st else 0, "tool_calls": o.tool_calls,
                             "model_calls": o.model_calls, "cost_usd": round(o.cost_usd, 5),
                             "deferred_reason": o.deferred_reason, "duration_s": o.duration_s})
                print(json.dumps({k: rows[-1][k] for k in ("arm", "task_id", "verdict", "booked", "blocks",
                                                            "tool_calls")}), flush=True)

        await asyncio.gather(*(one(i, t) for i, t in enumerate(tasks, 1)))
    events.close()
    if hasattr(client, "close"):
        await client.close()
    args.spent = spent[0]
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--tasks", default="data/tasks/tasks_messy_v1.jsonl")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--arms", default="original,hardened")
    ap.add_argument("--provider", default="ollama", choices=["ollama", "meta"])
    ap.add_argument("--model", default="gemma4-12b-16k:latest")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--cap-usd", type=float, default=None)
    args = ap.parse_args()
    args.spent = 0.0
    tasks = [json.loads(x) for x in Path(args.tasks).read_text().splitlines()]
    tasks = [t for t in tasks if args.split == "all" or t["split"] == args.split]
    arms = args.arms.split(",")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    if args.provider == "meta":
        per_goal = cost_usd(args.model, TYPICAL_INPUT, TYPICAL_CACHED, TYPICAL_OUTPUT)
        print(f"model spend estimate: ~${per_goal * len(tasks) * len(arms):.2f} for {len(tasks)} tasks x {len(arms)} "
              f"arms; stops at ${args.cap_usd}", flush=True)
        if args.cap_usd is None:
            raise SystemExit("--cap-usd is required with --provider meta")
    t0 = time.monotonic()
    rows = []
    for arm in arms:
        rows += await run_arm(arm, tasks, args, out)
        (out / "results.json").write_text(json.dumps(rows, indent=2, default=str))
    for arm in arms:
        got = [r for r in rows if r["arm"] == arm]
        print(arm, len(got), dict(Counter(r["verdict"] for r in got)))
    print(f"spent ${args.spent:.2f} in {time.monotonic() - t0:.0f} s; results {out / 'results.json'}")


if __name__ == "__main__":
    asyncio.run(main())
