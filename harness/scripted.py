"""Careful scripted client for one goal. Two policy versions; the manifest picks one.

v1, the original policy (kept unchanged so explore-v1 trials stay reproducible):

1. Read the customer's appointments and find the one the task describes.
2. Check each day of the window with the single-day lookup; take the first open slot.
3. If a day lookup still fails after its retries, fall back once to a broad search over the whole window
   (all regions, ranked from the customer's region) and keep slots whose technician covers that region.
4. Reschedule. On 409, re-check once and pick another slot.
5. Read the appointment back to confirm. If no slot exists, defer.

Every request uses the adapter with a `RetryPolicy` (up to 2 retries on timeout, 5xx or 429; full-jitter backoff,
base 0.5 s, cap 8 s; Retry-After honored up to 30 s). A fixed think time, the agents' median healthy step time,
precedes each step. Budget: 20 HTTP requests and 240 s.

robust_v2, the strongest conventional baseline we can write for this workflow (audit 2026-10-27, finding 7: v1
abandons a goal when its first read fails three times, which is a weak baseline, not a careful one):
- Same steps and fallback as v1, but no fixed retry count: every operation retries on timeout, 5xx or 429 until the
  request budget or the goal deadline, with full-jitter backoff (base 0.5 s, cap 8 s) and Retry-After honored.
- The request budget is enforced before every HTTP attempt, internal retries included.
- Uncertain write (timeout or 5xx on reschedule): read the appointment back first; if the slot changed, the write
  applied and the goal is done; otherwise retry the write with the same idempotency key.
- Pacing: fixed `think_time_s` (0 = unpaced), or a seeded draw from recorded agent step gaps (`think_samples`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import time
from dataclasses import dataclass

from harness.adapter import GoalScope, GoalState, RetryPolicy, ToolAdapter
from harness.agent import GoalOutcome, finish
from harness.events import EventLog
from harness.probe import all_regions


@dataclass
class ScriptedConfig:
    think_time_s: float
    policy: str = "v1"  # v1 | robust_v2
    think_samples: list[float] | None = None  # robust_v2 pacing from recorded agent step gaps (seeded draw)
    max_retries: int = 2
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    retry_after_cap_s: float = 30.0
    request_budget: int = 20
    goal_deadline_s: float = 240.0
    seed: int = 13


class BudgetExhausted(Exception):
    pass


async def run_goal(task: dict, goal_n: int, trial_id: str, adapter: ToolAdapter, events: EventLog,
                   cfg: ScriptedConfig, sleep=asyncio.sleep) -> GoalOutcome:
    if cfg.policy == "robust_v2":
        return await run_goal_v2(task, goal_n, trial_id, adapter, events, cfg, sleep)
    if cfg.policy != "v1":
        raise ValueError(f"unknown scripted policy {cfg.policy!r}")
    scope = GoalScope(trial_id, goal_n, task["customer_id"], task["appointment_id"], task["allowed_regions"],
                      actor_kind="scripted")
    state = GoalState()
    out = GoalOutcome(goal_id=scope.goal_id, task_id=task["task_id"], end_reason="gave_up")
    retry = RetryPolicy(cfg.max_retries, cfg.backoff_base_s, cfg.backoff_cap_s, cfg.retry_after_cap_s,
                        rng=random.Random(f"{cfg.seed}:{trial_id}:{goal_n}"))
    ids = {"goal_id": scope.goal_id, "actor_id": scope.actor_id, "actor_kind": "scripted"}
    events.emit("goal.started", **ids, task_id=task["task_id"])
    t_start = time.monotonic()
    deadline = t_start + cfg.goal_deadline_s

    async def step(name: str, args: dict) -> dict:
        if state.attempts >= cfg.request_budget:
            raise BudgetExhausted
        await sleep(cfg.think_time_s)
        return await adapter.call(scope, state, name, args, retry=retry, deadline=deadline)

    def days() -> list[str]:
        d1, d2 = dt.date.fromisoformat(task["d1"]), dt.date.fromisoformat(task["d2"])
        return [(d1 + dt.timedelta(days=i)).isoformat() for i in range((d2 - d1).days + 1)]

    async def find_slots(region: str, exclude: set[int], used_broad: list[bool]) -> list[int] | None:
        """Open slot IDs in the window, in policy order; None if every lookup failed."""
        any_ok = False
        for d in days():
            res = await step("check_day_availability", {"region": region, "date": d, "service": task["service"]})
            if "error" not in res:
                any_ok = True
                slots = [s["slot_id"] for s in res.get("slots", []) if s["slot_id"] not in exclude]
                if slots:
                    return slots
                continue
            if not used_broad[0]:  # the day lookup still failed after its retries: one broad search
                used_broad[0] = True
                res = await step("search_availability", {
                    "service": task["service"], "date_from": task["d1"], "date_to": task["d2"],
                    "regions": all_regions({"customer_region": region})})
                if "error" not in res:
                    slots = [s["slot_id"] for s in res.get("slots", [])
                             if region in s.get("covers_regions", []) and s["slot_id"] not in exclude]
                    if slots:
                        return slots
                    any_ok = True
        return [] if any_ok else None

    async def policy() -> None:
        listing = await step("get_customer_appointments", {"customer_id": task["customer_id"]})
        if "error" in listing:
            return
        region = listing["region"]
        match = [a for a in listing["appointments"] if a["status"] == "scheduled"
                 and a["service"] == task["service"] and a["date"] == task["current_date"]]
        if len(match) != 1:
            return
        appt = match[0]["appointment_id"]
        used_broad = [False]
        slots = await find_slots(region, set(), used_broad)
        if slots is None:
            return
        if not slots:
            await step("defer_goal", {"reason": "no open qualifying slot in the requested window"})
            out.end_reason = "deferred"
            return
        res = await step("reschedule_appointment", {"appointment_id": appt, "slot_id": slots[0]})
        if "error" in res and res["error"]["type"] == "http_409":
            slots = await find_slots(region, {slots[0]}, used_broad)  # re-check once, pick another slot
            if not slots:
                return
            res = await step("reschedule_appointment", {"appointment_id": appt, "slot_id": slots[0]})
        if "error" in res:
            return
        out.end_reason = "rescheduled"
        await step("get_appointment", {"appointment_id": appt})

    try:
        async with asyncio.timeout(cfg.goal_deadline_s):
            await policy()
    except TimeoutError:
        out.end_reason, out.limit = ("rescheduled", None) if state.write_applied else ("limit_reached", "deadline")
    except BudgetExhausted:
        out.end_reason, out.limit = ("rescheduled", None) if state.write_applied else ("limit_reached", "requests")
    finish(out, state, t_start)
    events.emit("goal.ended", **ids, task_id=task["task_id"], outcome=out.end_reason, limit=out.limit,
                change_id=out.change_id, deferred_reason=out.deferred_reason, tool_calls=out.tool_calls,
                attempts=state.attempts, duration_s=out.duration_s, invalid_arguments=state.invalid_arguments,
                gate_rejections=state.gate_rejections)
    return out


UNCERTAIN = ("timeout", "connection_error")


async def run_goal_v2(task: dict, goal_n: int, trial_id: str, adapter: ToolAdapter, events: EventLog,
                      cfg: ScriptedConfig, sleep=asyncio.sleep) -> GoalOutcome:
    scope = GoalScope(trial_id, goal_n, task["customer_id"], task["appointment_id"], task["allowed_regions"],
                      actor_kind="scripted")
    state = GoalState()
    out = GoalOutcome(goal_id=scope.goal_id, task_id=task["task_id"], end_reason="gave_up")
    rng = random.Random(f"{cfg.seed}:{trial_id}:{goal_n}")
    retry = RetryPolicy(None, cfg.backoff_base_s, cfg.backoff_cap_s, cfg.retry_after_cap_s, rng=rng)
    ids = {"goal_id": scope.goal_id, "actor_id": scope.actor_id, "actor_kind": "scripted"}
    events.emit("goal.started", **ids, task_id=task["task_id"], policy="robust_v2")
    t_start = time.monotonic()
    deadline = t_start + cfg.goal_deadline_s

    async def step(name: str, args: dict, retrying: bool = True) -> dict:
        if state.attempts >= cfg.request_budget:
            raise BudgetExhausted
        think = rng.choice(cfg.think_samples) if cfg.think_samples else cfg.think_time_s
        if think > 0:
            await sleep(think)
        res = await adapter.call(scope, state, name, args, retry=retry if retrying else None, deadline=deadline,
                                 attempt_budget=cfg.request_budget)
        if res.get("error", {}).get("type") == "budget_exhausted":
            raise BudgetExhausted
        return res

    def days() -> list[str]:
        d1, d2 = dt.date.fromisoformat(task["d1"]), dt.date.fromisoformat(task["d2"])
        return [(d1 + dt.timedelta(days=i)).isoformat() for i in range((d2 - d1).days + 1)]

    async def find_slots(region: str, exclude: set[int], used_broad: list[bool]) -> list[int] | None:
        any_ok = False
        for d in days():
            res = await step("check_day_availability", {"region": region, "date": d, "service": task["service"]})
            if "error" not in res:
                any_ok = True
                slots = [s["slot_id"] for s in res.get("slots", []) if s["slot_id"] not in exclude]
                if slots:
                    return slots
                continue
            if not used_broad[0]:
                used_broad[0] = True
                res = await step("search_availability", {
                    "service": task["service"], "date_from": task["d1"], "date_to": task["d2"],
                    "regions": all_regions({"customer_region": region})})
                if "error" not in res:
                    slots = [s["slot_id"] for s in res.get("slots", [])
                             if region in s.get("covers_regions", []) and s["slot_id"] not in exclude]
                    if slots:
                        return slots
                    any_ok = True
        return [] if any_ok else None

    async def write(appt: int, slot: int) -> str:
        """ok | conflict | failed. Retries uncertain writes after reading the appointment back."""
        while True:
            res = await step("reschedule_appointment", {"appointment_id": appt, "slot_id": slot}, retrying=False)
            if "error" not in res:
                return "ok"
            kind = res["error"]["type"]
            if kind == "http_409":
                return "conflict"
            if kind not in UNCERTAIN and not (kind.startswith("http_5") or kind == "http_429"):
                return "failed"  # 4xx other than 409/429: retrying cannot help
            wait = retry.wait_s(state.attempts, res["error"].get("retry_after_s"))
            if time.monotonic() + wait >= deadline:
                return "failed"
            await sleep(wait)
            # 503 and 429 mean the request was refused before any work; anything else may have committed.
            if kind in UNCERTAIN or (kind.startswith("http_5") and kind != "http_503"):
                check = await step("get_appointment", {"appointment_id": appt})
                if "error" not in check and check.get("slot_id") == slot:
                    state.write_applied = True  # the earlier attempt committed; its response was lost
                    return "ok"

    async def policy() -> None:
        while True:  # the first read is retried like any other step, never abandoned after a fixed count
            listing = await step("get_customer_appointments", {"customer_id": task["customer_id"]})
            if "error" not in listing:
                break
            if listing["error"]["type"] not in UNCERTAIN and not listing["error"]["type"].startswith("http_5"):
                return
        region = listing["region"]
        match = [a for a in listing["appointments"] if a["status"] == "scheduled"
                 and a["service"] == task["service"] and a["date"] == task["current_date"]]
        if len(match) != 1:
            return
        appt = match[0]["appointment_id"]
        used_broad = [False]
        exclude: set[int] = set()
        slots = await find_slots(region, exclude, used_broad)
        if slots is None:
            return
        if not slots:
            await step("defer_goal", {"reason": "no open qualifying slot in the requested window"})
            out.end_reason = "deferred"
            return
        result = await write(appt, slots[0])
        if result == "conflict":
            exclude.add(slots[0])
            slots = await find_slots(region, exclude, used_broad)
            if not slots:
                return
            result = await write(appt, slots[0])
        if result != "ok":
            return
        out.end_reason = "rescheduled"
        await step("get_appointment", {"appointment_id": appt})

    try:
        async with asyncio.timeout(cfg.goal_deadline_s):
            await policy()
    except TimeoutError:
        out.end_reason, out.limit = ("rescheduled", None) if state.write_applied else ("limit_reached", "deadline")
    except BudgetExhausted:
        out.end_reason, out.limit = ("rescheduled", None) if state.write_applied else ("limit_reached", "requests")
    finish(out, state, t_start)
    events.emit("goal.ended", **ids, task_id=task["task_id"], outcome=out.end_reason, limit=out.limit,
                change_id=out.change_id, deferred_reason=out.deferred_reason, tool_calls=out.tool_calls,
                attempts=state.attempts, duration_s=out.duration_s, invalid_arguments=state.invalid_arguments,
                gate_rejections=state.gate_rejections, policy="robust_v2")
    return out
