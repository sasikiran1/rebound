"""Refusal probe (docs/policy_probe_protocol.md): what does an agent do right after a 503 with Retry-After?

Usage: python scripts/policy_probe.py <out_dir> --arms base,wait,... --n-tasks 30 --provider meta|ollama
         [--model NAME] [--concurrency N] [--cap-usd X]

FieldDesk refuses the first PROBE_REFUSE_FIRST requests of every goal with 503 and Retry-After 2, so every goal meets
refusals without load or a fault. No gate, no background traffic, fresh database per arm. Arms differ only in the
tool set (with or without a `wait` tool), the server's agent policy (on: refusals state the policy), the conversation
continuation, and an injected pause before each model call (pacing sweep). The SQL oracle judges every goal.
--provider meta spends real money: the estimate is printed first, and no new goal starts once --cap-usd is reached.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import asyncpg

sys.path.insert(0, ".")
from harness import dbadmin, settings, sut  # noqa: E402
from harness.adapter import ToolAdapter, load_tools, new_http_client  # noqa: E402
from harness.agent import AgentConfig, run_goal  # noqa: E402
from harness.events import EventLog  # noqa: E402
from harness.manifest import load  # noqa: E402
from harness.ollama import OllamaClient  # noqa: E402
from harness.provider import Provider, cost_usd, make_client  # noqa: E402
from harness.single import TYPICAL_CACHED, TYPICAL_INPUT, TYPICAL_OUTPUT  # noqa: E402
from oracle.verdicts import verdict_for  # noqa: E402

REFUSE_FIRST = 2
V1, WAIT = "prompts/tools_v1.json", "prompts/tools_v1_wait.json"
FH, PRID = "full_history", "previous_response_id"
# name: (tool file, agent policy on, continuation, pause in seconds before each model call)
ARMS = {
    "base": (V1, False, FH, 0.0),
    "wait": (WAIT, False, FH, 0.0),
    "wait_policy": (WAIT, True, FH, 0.0),
    "policy_nowait": (V1, True, FH, 0.0),
    "base_prid": (V1, False, PRID, 0.0),
    "base_pause1": (V1, False, FH, 1.0),
    "base_pause2": (V1, False, FH, 2.0),
    "base_pause3": (V1, False, FH, 3.0),
}


class PausedProvider(Provider):
    """Pacing sweep: a fixed pause before every model call, outside the model's measured latency."""

    def __init__(self, client, pause_s: float):
        super().__init__(client)
        self.pause_s = pause_s

    async def create(self, **kwargs):
        if self.pause_s:
            await asyncio.sleep(self.pause_s)
        return await super().create(**kwargs)


def refusal_gaps(client_log: Path) -> dict[str, list[dict]]:
    """Per goal: each refused attempt and the time from its response to the goal's next attempt being sent."""
    attempts = defaultdict(list)
    for line in client_log.read_text().splitlines():
        e = json.loads(line)
        if e["event_type"] == "http.attempt":
            end = e["ts_mono_ns"] / 1e9
            attempts[e["goal_id"]].append({"start": end - e["latency_ms"] / 1000, "end": end,
                                           "status": e["http_status"], "retry_after_s": e["retry_after_s"]})
    out = {}
    for goal, rows in attempts.items():
        rows.sort(key=lambda r: r["start"])
        gaps = []
        for a, b in zip(rows, rows[1:] + [None], strict=True):
            if a["status"] in (429, 503) and a["retry_after_s"] is not None:
                gaps.append({"retry_after_s": a["retry_after_s"],
                             "gap_s": None if b is None else round(b["start"] - a["end"], 3)})
        out[goal] = gaps
    return out


def summarize(rows: list[dict]) -> dict:
    gaps = [g for r in rows for g in r["refusals"]]
    followed = [g for g in gaps if g["gap_s"] is not None]
    early = [g for g in followed if g["gap_s"] < g["retry_after_s"]]
    return {
        "goals": len(rows),
        "verdicts": dict(Counter(r["verdict"] for r in rows)),
        "end_reasons": dict(Counter(r["end_reason"] for r in rows)),
        "refusals": len(gaps),
        "refusals_followed_by_a_request": len(followed),
        "early_requests": len(early),
        "early_share": round(len(early) / len(followed), 3) if followed else None,
        "gap_s_median": statistics.median(g["gap_s"] for g in followed) if followed else None,
        "goals_calling_wait": sum(r["waits"] > 0 for r in rows),
        "wait_calls": sum(r["waits"] for r in rows),
        "model_step_s_median": statistics.median(s for r in rows for s in r["model_s"]) if rows else None,
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
    }


async def run_arm(arm: str, tasks: list[dict], args, out: Path) -> list[dict]:
    tool_file, policy_on, continuation, pause_s = ARMS[arm]
    await dbadmin.clone_trial_db()
    m = load("manifests/frozen/eval_v1.yaml")
    env = {**sut.fielddesk_env(m, False), "AGENT_POLICY": "on" if policy_on else "off",
           "PROBE_REFUSE_FIRST": str(REFUSE_FIRST)}
    await asyncio.to_thread(sut.restart_fielddesk, env)
    folder = out / arm
    events = EventLog(folder, experiment_id="policy-probe", trial_id=f"pp_{arm}", condition="agent", gate="off",
                      config_hash=None)
    tools = load_tools(tool_file)
    if args.provider == "ollama":
        client = OllamaClient({"temperature": 0, "seed": 41, "num_ctx": 16384}, think=False,
                              raw_dir=folder / "ollama_raw")
    else:
        client = make_client()
    provider = PausedProvider(client, pause_s)
    cfg = AgentConfig(model=args.model, prompt_path="prompts/agent_v1.md", continuation=continuation,
                      max_output_tokens=2048, max_model_calls=16, max_tool_calls=14, goal_deadline_s=240)
    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []
    states: dict = {}

    class Capturing(ToolAdapter):
        async def call(self, scope, state, name, raw_args, *a, **kw):
            states[scope.goal_id] = state
            return await super().call(scope, state, name, raw_args, *a, **kw)

    async with new_http_client() as http:
        adapter = Capturing(settings.FIELDDESK_URL, http, tools, events)

        async def one(n: int, t: dict) -> None:
            async with sem:
                if args.cap_usd is not None and args.spent >= args.cap_usd:
                    rows.append({"arm": arm, "task_id": t["task_id"], "verdict": "not_run_budget"})
                    return
                o = await run_goal(t, n, f"pp_{arm}", provider, adapter, events, tools, cfg)
                args.spent += o.cost_usd
                conn = await asyncpg.connect(settings.oracle_dsn())
                try:
                    v = await verdict_for(conn, t, o.end_reason)
                finally:
                    await conn.close()
                st = states.get(o.goal_id)
                rows.append({"arm": arm, "goal_id": o.goal_id, "task_id": t["task_id"], "verdict": v["verdict"],
                             "end_reason": o.end_reason, "limit": o.limit, "deferred_reason": o.deferred_reason,
                             "model_calls": o.model_calls, "tool_calls": o.tool_calls,
                             "model_s": [s["model_ms"] / 1000 for s in o.steps], "attempts": o.attempts,
                             "waits": st.waits if st else 0, "wait_s": st.wait_s if st else 0.0,
                             "cost_usd": round(o.cost_usd, 5), "duration_s": o.duration_s})
                print(json.dumps({k: rows[-1][k] for k in ("arm", "task_id", "verdict", "waits", "model_calls")}),
                      flush=True)

        await asyncio.gather(*(one(i, t) for i, t in enumerate(tasks, 1)))
    events.close()
    sut.copy_logs(folder)
    if hasattr(client, "close"):
        await client.close()
    gaps = refusal_gaps(folder / "client.jsonl")
    for r in rows:
        r["refusals"] = gaps.get(r.get("goal_id"), [])
    return [r for r in rows if r["verdict"] != "not_run_budget"], [r for r in rows if r["verdict"] == "not_run_budget"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--n-tasks", type=int, default=30)
    ap.add_argument("--provider", default="ollama", choices=["ollama", "meta"])
    ap.add_argument("--model", default="gemma4-12b-16k:latest")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--cap-usd", type=float, default=None)
    args = ap.parse_args()
    args.spent = 0.0
    arms = args.arms.split(",")
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise SystemExit(f"unknown arms {unknown}")
    # The first n tasks of task set B that need a reschedule: the same tasks, in the same order, in every arm.
    tasks = [json.loads(x) for x in Path("data/tasks/tasks_eval_B.jsonl").read_text().splitlines()]
    tasks = [t for t in tasks if t["expected"] == "reschedule"][: args.n_tasks]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    if args.provider == "meta":
        if args.cap_usd is None:
            raise SystemExit("--cap-usd is required with --provider meta")
        per_goal = cost_usd(args.model, TYPICAL_INPUT, TYPICAL_CACHED, TYPICAL_OUTPUT)
        print(f"model spend estimate: ~${per_goal * len(tasks) * len(arms):.2f} for {len(tasks)} tasks x {len(arms)} "
              f"arms (eval-v1 token mix; refusals add calls); stops at ${args.cap_usd}", flush=True)
    (out / "config.json").write_text(json.dumps({"arms": {a: ARMS[a] for a in arms}, "refuse_first": REFUSE_FIRST,
                                                 "tasks": [t["task_id"] for t in tasks], "provider": args.provider,
                                                 "model": args.model, "cap_usd": args.cap_usd}, indent=2))
    t0 = time.monotonic()
    rows, skipped, summary = [], [], {}
    for arm in arms:
        got, missed = await run_arm(arm, tasks, args, out)
        rows += got
        skipped += missed
        summary[arm] = summarize(got)
        (out / "results.json").write_text(json.dumps(rows, indent=2, default=str))
        (out / "summary.json").write_text(json.dumps({"arms": summary, "skipped_for_budget": len(skipped),
                                                      "spent_usd": round(args.spent, 4)}, indent=2))
        print(arm, json.dumps(summary[arm]), flush=True)
    print(f"spent ${args.spent:.2f} in {time.monotonic() - t0:.0f} s; skipped for budget: {len(skipped)}; "
          f"summary {out / 'summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
