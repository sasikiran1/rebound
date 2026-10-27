"""Day 1 single-goal runs: model verification and one goal end to end with its oracle verdict."""

from __future__ import annotations

import datetime as dt
import json

import asyncpg

from harness import settings
from harness.adapter import ToolAdapter, load_tools, new_http_client
from harness.agent import AgentConfig, run_goal
from harness.events import EventLog
from harness.probe import load_tasks
from harness.provider import PRICES, Provider, ProviderFailed, cost_usd, make_client
from oracle.verdicts import verdict_for


def estimate(cfg: AgentConfig) -> tuple[float, float]:
    """(typical, cap) USD for one goal. Typical assumes ~6 calls of ~2.5k input / 300 output tokens."""
    typical = cost_usd(cfg.model, 6 * 2500, 0, 6 * 300)
    cap = cost_usd(cfg.model, cfg.max_input_tokens, 0, cfg.max_model_calls * cfg.max_output_tokens)
    return typical, cap


async def verify_model(model: str) -> None:
    """One minimal call with one tool, then one continuation via previous_response_id."""
    if model not in PRICES:
        raise SystemExit(f"no price for {model}")
    tool = next(t for t in load_tools() if t["name"] == "get_appointment")
    provider = Provider(make_client())
    common = dict(model=model, tools=[tool], reasoning={"effort": "low"}, max_output_tokens=2048,
                  parallel_tool_calls=False, tool_choice="auto", prompt_cache_key="rebound-verify-v1", store=True)
    print(f"[1/2] {model}: minimal call with one tool (estimated < $0.002)")
    try:
        r1 = (await provider.create(input="Look up appointment 1 with the tool, then report its status.", **common))
    except ProviderFailed as exc:
        raise SystemExit(f"provider failed: {exc.status} {exc.message}") from exc
    resp = r1.response
    print(f"  id={resp.id} status={getattr(resp, 'status', None)} latency={r1.latency_ms:.0f} ms")
    print(f"  output items: {[item.type for item in resp.output]}")
    print(f"  usage: {json.dumps(resp.usage.model_dump() if resp.usage else None)}")
    total = _print_cost(model, resp.usage)
    call = next((item for item in resp.output if item.type == "function_call"), None)
    if call is None:
        print("  no function call returned; cannot test continuation")
        return
    print(f"  function_call: {call.name}({call.arguments})")
    print("[2/2] continuation with previous_response_id + function_call_output")
    fake = {"appointment_id": 1, "status": "scheduled", "service": "hvac_repair", "date": "2026-11-05"}
    try:
        r2 = await provider.create(
            previous_response_id=resp.id,
            input=[{"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(fake)}],
            **common,
        )
    except ProviderFailed as exc:
        print(f"  continuation FAILED: {exc.status} {exc.message}")
        return
    resp2 = r2.response
    print(f"  id={resp2.id} status={getattr(resp2, 'status', None)} latency={r2.latency_ms:.0f} ms")
    print(f"  usage: {json.dumps(resp2.usage.model_dump() if resp2.usage else None)}")
    total += _print_cost(model, resp2.usage)
    text = " ".join(c.text for item in resp2.output if item.type == "message" for c in item.content
                    if getattr(c, "type", "") == "output_text")
    print(f"  text: {text[:200]!r}")
    print(f"total estimated cost: ${total:.5f}")


def _print_cost(model: str, usage) -> float:
    if usage is None:
        print("  cost: usage missing")
        return 0.0
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    c = cost_usd(model, usage.input_tokens, cached, usage.output_tokens)
    print(f"  cost: ${c:.6f} (input {usage.input_tokens}, cached {cached}, output {usage.output_tokens})")
    return c


async def run_one(task_id: str | None, model: str) -> None:
    tasks = load_tasks()
    task = next(t for t in tasks if t["task_id"] == task_id) if task_id else next(
        t for t in tasks if t["kind"] == "narrow_1day"
    )
    cfg = AgentConfig(model=model)
    typical, cap = estimate(cfg)
    print(f"goal {task['task_id']} ({task['kind']}, expected {task['expected']}) on {model}")
    print(f"estimated cost: ~${typical:.3f} typical, ${cap:.3f} at the per-goal limits")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    experiment_id, trial_id = "exp_day1_smoke", f"tr_{stamp}"
    events = EventLog(f"runs/{experiment_id}/{trial_id}", experiment_id=experiment_id, trial_id=trial_id,
                      condition="agent", gate="off", config_hash=None)
    tools = load_tools()

    def show(step: dict) -> None:
        print(f"  {step['model_call']}: model {step['model_ms']} ms, in {step['input_tokens']} "
              f"(cached {step['cached_tokens']}) out {step['output_tokens']} (reasoning {step['reasoning_tokens']}) "
              f"${step['cost_usd']:.5f}")
        for t in step["tools"]:
            print(f"      -> {t['name']}({t['arguments']}) = {t['status']} in {t['latency_ms']} ms")
        if step["text"]:
            print(f"      text: {step['text'][:300]!r}")

    async with new_http_client() as http:
        adapter = ToolAdapter(settings.FIELDDESK_URL, http, tools, events)
        out = await run_goal(task, 1, trial_id, Provider(make_client()), adapter, events, tools, cfg, on_step=show)

    conn = await asyncpg.connect(settings.oracle_dsn())
    try:
        v = await verdict_for(conn, task, out.end_reason)
    finally:
        await conn.close()
    events.emit("oracle.verdict", goal_id=out.goal_id, task_id=task["task_id"], verdict=v["verdict"],
                end_reason=v["end_reason"], completed_at=v["completed_at"], facts=v["facts"])
    events.close()
    print(f"ended: {out.end_reason}{' (' + out.limit + ')' if out.limit else ''} after {out.duration_s} s, "
          f"{out.model_calls} model calls, {out.tool_calls} tool calls")
    print(f"tokens: input {out.input_tokens} (cached {out.cached_tokens}), output {out.output_tokens} "
          f"(reasoning {out.reasoning_tokens}); estimated cost ${out.cost_usd:.5f}")
    print(f"oracle: {v['verdict']}  completed_at={v['completed_at']}")
    print(f"events: runs/{experiment_id}/{trial_id}/")
