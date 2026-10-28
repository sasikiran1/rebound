"""Live agent for one goal: Responses API, conversation continued with previous_response_id.

The prompt describes the goal and tools only. The agent decides every step, including retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from harness.adapter import GoalScope, GoalState, ToolAdapter
from harness.events import EventLog, model_call_id
from harness.provider import Provider, ProviderFailed, cost_usd


@dataclass
class AgentConfig:
    model: str = "muse-spark-1.3"
    reasoning_effort: str = "low"
    max_output_tokens: int = 2048
    prompt_cache_key: str = "rebound-agent-v1"
    prompt_path: str = "prompts/agent_v1.md"
    max_model_calls: int = 16
    max_tool_calls: int = 14
    goal_deadline_s: float = 240.0
    max_input_tokens: int = 80_000
    # previous_response_id: provider-stored conversation.
    # full_history: every call carries the goal's whole conversation from the runner's own records, store=False.
    # Exploration found the provider losing about 3% of stored responses deep in a chain (docs/results.md).
    continuation: str = "previous_response_id"


@dataclass
class GoalOutcome:
    goal_id: str
    task_id: str
    end_reason: str  # rescheduled | deferred | gave_up | limit_reached | provider_failed
    limit: str | None = None
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    change_id: int | None = None
    deferred_reason: str | None = None
    provider_error: str | None = None
    gate_rejections: int = 0
    attempts: int = 0
    steps: list[dict] = field(default_factory=list)


def render_prompt(path: str, task: dict) -> str:
    return Path(path).read_text().format(
        goal_text=task["goal_text"], customer_id=task["customer_id"], d1=task["d1"], d2=task["d2"]
    )


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _usage(response) -> dict:
    u = getattr(response, "usage", None)
    if u is None:
        return {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    in_details = getattr(u, "input_tokens_details", None)
    out_details = getattr(u, "output_tokens_details", None)
    return {
        "input_tokens": u.input_tokens or 0,
        "cached_tokens": (getattr(in_details, "cached_tokens", 0) or 0) if in_details else 0,
        "output_tokens": u.output_tokens or 0,
        "reasoning_tokens": (getattr(out_details, "reasoning_tokens", 0) or 0) if out_details else 0,
    }


def _visible_text(response) -> str:
    parts = []
    for item in response.output:
        if item.type == "message":
            parts += [c.text for c in item.content if getattr(c, "type", "") == "output_text"]
    return "\n".join(parts)


async def run_goal(task: dict, goal_n: int, trial_id: str, provider: Provider, adapter: ToolAdapter,
                   events: EventLog, tools: list[dict], cfg: AgentConfig, on_step=None) -> GoalOutcome:
    scope = GoalScope(trial_id, goal_n, task["customer_id"], task["appointment_id"], task["allowed_regions"])
    state = GoalState()
    out = GoalOutcome(goal_id=scope.goal_id, task_id=task["task_id"], end_reason="gave_up")
    instructions = render_prompt(cfg.prompt_path, task)
    prompt_hash = sha256_text(instructions)
    tools_hash = sha256_text(json.dumps(tools, sort_keys=True))
    ids = {"goal_id": scope.goal_id, "actor_id": scope.actor_id, "actor_kind": "agent"}
    events.emit("goal.started", **ids, task_id=task["task_id"])
    t_start = time.monotonic()

    full_history = cfg.continuation == "full_history"

    async def loop() -> None:
        next_input: list = [{"role": "user", "content": "Please handle the task."}]
        history: list = []  # full_history mode: every input item and model output item so far
        previous_id = None
        while True:
            if out.model_calls >= cfg.max_model_calls:
                out.end_reason, out.limit = "limit_reached", "model_calls"
                return
            if out.input_tokens >= cfg.max_input_tokens:
                out.end_reason, out.limit = "limit_reached", "input_tokens"
                return
            out.model_calls += 1
            mc_id = model_call_id(goal_n, out.model_calls)
            if full_history:
                history += next_input
                call_input, continuation = list(history), {"store": False}
            else:
                call_input, continuation = next_input, {"previous_response_id": previous_id, "store": True}
            try:
                res = await provider.create(
                    model=cfg.model, instructions=instructions, input=call_input, tools=tools,
                    reasoning={"effort": cfg.reasoning_effort}, max_output_tokens=cfg.max_output_tokens,
                    parallel_tool_calls=False, tool_choice="auto", prompt_cache_key=cfg.prompt_cache_key,
                    **continuation,
                )
            except ProviderFailed as exc:
                events.emit("model.call", **ids, model_call_id=mc_id, model=cfg.model, status="provider_failed",
                            provider_status=exc.status, provider_message=exc.message, provider_retries=exc.retries,
                            provider_wait_s=exc.wait_s)
                out.end_reason, out.provider_error = "provider_failed", f"{exc.status}: {exc.message}"
                return
            resp = res.response
            usage = _usage(resp)
            call_cost = cost_usd(cfg.model, usage["input_tokens"], usage["cached_tokens"], usage["output_tokens"])
            out.input_tokens += usage["input_tokens"]
            out.cached_tokens += usage["cached_tokens"]
            out.output_tokens += usage["output_tokens"]
            out.reasoning_tokens += usage["reasoning_tokens"]
            out.cost_usd += call_cost
            events.emit(
                "model.call", **ids, model_call_id=mc_id, model=cfg.model, prompt_hash=prompt_hash,
                tools_hash=tools_hash, **usage, cost_usd=round(call_cost, 6), latency_ms=round(res.latency_ms, 1),
                status="ok", provider_retries=res.retries, provider_wait_s=res.wait_s, response_id=resp.id,
                previous_response_id=None if full_history else previous_id, continuation=cfg.continuation,
                input_items=len(call_input), response_status=getattr(resp, "status", None),
                done_reason=getattr(resp, "done_reason", None), context_used=getattr(resp, "context_used", None),
                num_ctx=getattr(resp, "num_ctx", None), server_s=getattr(resp, "server_s", None),
            )
            previous_id = resp.id
            calls = [item for item in resp.output if item.type == "function_call"]
            text = _visible_text(resp)
            if full_history:  # visible text and tool calls only; hidden reasoning is not carried over
                if text:
                    history.append({"role": "assistant", "content": text})
                history += [{"type": "function_call", "call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                            for c in calls]
            events.emit("agent.message", **ids, model_call_id=mc_id, text=text,
                        tool_calls=[{"name": c.name, "arguments": c.arguments} for c in calls])
            step = {"model_call": mc_id, "model_ms": round(res.latency_ms), **usage, "cost_usd": call_cost,
                    "text": text, "tools": []}
            out.steps.append(step)
            if not calls:
                out.end_reason = "rescheduled" if state.write_applied else "gave_up"
                if on_step:
                    on_step(step)
                return
            next_input = []
            for call in calls:
                if state.tool_calls >= cfg.max_tool_calls:
                    out.end_reason, out.limit = "limit_reached", "tool_calls"
                    if on_step:
                        on_step(step)
                    return
                t0 = time.perf_counter()
                result = await adapter.call(scope, state, call.name, call.arguments, mc_id)
                status = result["error"]["type"] if "error" in result else "ok"
                step["tools"].append({"name": call.name, "arguments": call.arguments, "status": status,
                                      "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
                next_input.append({"type": "function_call_output", "call_id": call.call_id,
                                   "output": json.dumps(result)})
            if on_step:
                on_step(step)
            if state.deferred_reason is not None:
                out.end_reason = "deferred"
                return

    try:
        async with asyncio.timeout(cfg.goal_deadline_s):
            await loop()
    except TimeoutError:
        out.end_reason, out.limit = "limit_reached", "deadline"
    finish(out, state, t_start)
    events.emit("goal.ended", **ids, task_id=task["task_id"], outcome=out.end_reason, limit=out.limit,
                change_id=out.change_id, deferred_reason=out.deferred_reason, model_calls=out.model_calls,
                tool_calls=out.tool_calls, input_tokens=out.input_tokens, cached_tokens=out.cached_tokens,
                output_tokens=out.output_tokens, cost_usd=round(out.cost_usd, 6), duration_s=out.duration_s,
                provider_error=out.provider_error, invalid_arguments=state.invalid_arguments,
                gate_rejections=state.gate_rejections, attempts=state.attempts)
    return out


def finish(out, state: GoalState, t_start: float) -> None:
    """Copy goal state into the outcome. A goal with no effect whose every request the gate refused is `rejected`."""
    out.tool_calls = state.tool_calls
    out.change_id = state.change_id
    out.deferred_reason = state.deferred_reason
    out.duration_s = round(time.monotonic() - t_start, 3)
    out.gate_rejections = state.gate_rejections
    out.attempts = state.attempts
    if (out.change_id is None and out.end_reason not in ("deferred", "provider_failed")
            and state.gate_rejections > 0 and state.non_rejected_attempts == 0):
        out.end_reason = "rejected"
