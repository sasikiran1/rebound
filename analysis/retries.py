"""Retry classification (lean: four classes, deterministic, at analysis).

Input: one goal's operations in order, each {tool, args_hash, status}. Each operation gets one label:

- `same_call_retry`: same tool and arguments as the operation just before it, which failed.
- `switch_to_broad_search`: `search_availability` after a failure of another availability tool in the goal
  (`check_day_availability` or `list_technicians`).
- `restart`: `get_customer_appointments` again after the goal had reached a later step.
- `other`: any other repeated or extra call: an exact repeat of an earlier call, any other search (a broadened
  search), or a further write after a failed write.
- `base`: everything else (the first call of each kind of work).

Rules are tried in that order; the first match wins. Defers are not classified.
"""

from __future__ import annotations

from collections import Counter

CLASSES = ("same_call_retry", "switch_to_broad_search", "restart", "other")
DAY_TOOLS = {"check_day_availability", "list_technicians"}


def classify_goal(ops: list[dict]) -> list[str]:
    labels: list[str] = []
    seen: set[tuple[str, str]] = set()
    failed_day_lookup = False
    failed_write = False
    for i, op in enumerate(ops):
        tool, key = op["tool"], (op["tool"], op["args_hash"])
        prev = ops[i - 1] if i else None
        if tool == "defer_goal":
            label = "base"
        elif prev is not None and prev["tool"] == tool and prev["args_hash"] == op["args_hash"] and prev[
            "status"
        ] != "ok":
            label = "same_call_retry"
        elif tool == "search_availability" and failed_day_lookup:
            label = "switch_to_broad_search"
        elif tool == "get_customer_appointments" and any(o["tool"] != tool for o in ops[:i]):
            label = "restart"
        elif key in seen or tool == "search_availability" and any(
            o["tool"] == "search_availability" for o in ops[:i]
        ) or (tool == "reschedule_appointment" and failed_write):
            label = "other"
        else:
            label = "base"
        labels.append(label)
        seen.add(key)
        if tool in DAY_TOOLS and op["status"] != "ok":
            failed_day_lookup = True
        if tool == "reschedule_appointment" and op["status"] != "ok":
            failed_write = True
    return labels


def count(labels: list[str]) -> Counter:
    c = Counter(labels)
    return Counter({k: c.get(k, 0) for k in (*CLASSES, "base")})
