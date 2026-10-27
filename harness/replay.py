"""Fixed-trace replay: re-send one healthy reference agent trial's requests, per goal, at their
recorded offsets from goal start. Open-loop: responses are ignored, nothing is retried, nothing reacts.

The trace is read from the reference trial's client.jsonl: every agent `http.attempt` (with the exact request it
sent) and every `defer_goal` operation, timed from the goal's `goal.started` event.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from harness.adapter import GoalScope, GoalState, ToolAdapter
from harness.agent import GoalOutcome, finish
from harness.events import EventLog, attempt_id, op_id


def load_trace(client_jsonl: str | Path) -> dict[int, list[dict]]:
    """goal_n -> steps [{offset_s, kind: http|defer, tool, request}] in send order."""
    starts: dict[str, int] = {}
    steps: dict[str, list[dict]] = {}
    for line in Path(client_jsonl).read_text().splitlines():
        e = json.loads(line)
        if e.get("actor_kind") != "agent":
            continue
        g = e["goal_id"]
        if e["event_type"] == "goal.started":
            starts[g] = e["ts_mono_ns"]
        elif e["event_type"] == "http.attempt":
            sent_ns = e["ts_mono_ns"] - int((e["latency_ms"] or 0) * 1e6)
            steps.setdefault(g, []).append({"t_ns": sent_ns, "kind": "http", "tool": e["tool"],
                                            "route": e["route"], "request": e["request"]})
        elif e["event_type"] == "tool.op" and e["status"] == "deferred":
            steps.setdefault(g, []).append({"t_ns": e["ts_mono_ns"], "kind": "defer", "tool": "defer_goal",
                                            "reason": e.get("reason")})
    out = {}
    for g, s in steps.items():
        s.sort(key=lambda x: x["t_ns"])
        out[int(g.split("_")[1])] = [{**x, "offset_s": (x.pop("t_ns") - starts[g]) / 1e9} for x in s]
    for g in starts:  # goals that made no request at all still exist in the trace
        out.setdefault(int(g.split("_")[1]), [])
    return out


async def run_goal(task: dict, goal_n: int, trial_id: str, steps: list[dict], adapter: ToolAdapter,
                   events: EventLog) -> GoalOutcome:
    scope = GoalScope(trial_id, goal_n, task["customer_id"], task["appointment_id"], task["allowed_regions"],
                      actor_kind="replay")
    state = GoalState()
    out = GoalOutcome(goal_id=scope.goal_id, task_id=task["task_id"], end_reason="gave_up")
    ids = {"goal_id": scope.goal_id, "actor_id": scope.actor_id, "actor_kind": "replay"}
    events.emit("goal.started", **ids, task_id=task["task_id"], trace_steps=len(steps))
    t_start = time.monotonic()
    pending = []

    async def send(k: int, st: dict) -> None:
        req = st["request"]
        headers = {"X-RB-Trial": trial_id, "X-RB-Goal": scope.goal_id, "X-RB-Op": op_id(goal_n, k),
                   "X-RB-Attempt": attempt_id(goal_n, k, 1), "X-RB-Actor": scope.actor_id}
        if req["body"] is not None:
            headers["Idempotency-Key"] = scope.idempotency_key
        result, attempt = await adapter.attempt(req["method"], adapter.url(req["path"]), req["params"], req["body"],
                                                headers)
        state.attempts += 1
        if attempt["http_status"] == 503 and attempt["retry_after_s"] is not None:
            state.gate_rejections += 1
        else:
            state.non_rejected_attempts += 1
        if attempt["status"] == "ok" and st["tool"] == "reschedule_appointment":
            state.write_applied, state.change_id = True, result.get("change_id")
        events.emit("http.attempt", **ids, logical_op_id=op_id(goal_n, k), attempt_id=headers["X-RB-Attempt"],
                    attempt_n=1, retry_layer="none", tool=st["tool"], route=st["route"], request=req,
                    timeout_ms=adapter.timeout_s * 1000, **attempt)

    for k, st in enumerate(steps, start=1):
        delay = t_start + st["offset_s"] - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        state.ops += 1
        if st["kind"] == "defer":
            state.deferred_reason = st.get("reason") or "replayed deferral"
            events.emit("tool.op", **ids, logical_op_id=op_id(goal_n, k), tool="defer_goal", status="deferred")
            continue
        pending.append(asyncio.create_task(send(k, st)))
    await asyncio.gather(*pending)
    if state.write_applied:
        out.end_reason = "rescheduled"
    elif state.deferred_reason is not None:
        out.end_reason = "deferred"
    finish(out, state, t_start)
    events.emit("goal.ended", **ids, task_id=task["task_id"], outcome=out.end_reason, change_id=out.change_id,
                attempts=state.attempts, duration_s=out.duration_s, gate_rejections=state.gate_rejections)
    return out
