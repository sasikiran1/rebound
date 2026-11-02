"""Independent exhaustive SQL check for small constructed cohorts.

Does not import the matching algorithm or trust API receipts. Exponential: <=8 goals.
"""

import json
import sqlite3


def optimum(graph):
    goals = list(graph)

    def search(index, used):
        if index == len(goals):
            return 0
        choices = graph[goals[index]] - used
        return max([search(index + 1, used)] + [1 + search(index + 1, used | {s}) for s in choices])

    return search(0, set())


def inspect(path, at):
    with sqlite3.connect(path) as conn:
        goals = {g: (set(json.loads(a)), expiry) for g, a, expiry in conn.execute("SELECT * FROM goals")}
        bookings = list(conn.execute("SELECT goal_id, slot_id FROM bookings"))
        slots = {r[0] for r in conn.execute("SELECT id FROM slots")}
    if len(goals) > 8:
        raise ValueError("exhaustive oracle supports at most 8 goals")
    valid = all(g in goals and s in goals[g][0] and s in slots for g, s in bookings)
    unique = len({g for g, _ in bookings}) == len(bookings) == len({s for _, s in bookings})
    completed = {g for g, _ in bookings}
    occupied = {s for _, s in bookings}
    active = {g: choices for g, (choices, expiry) in goals.items() if expiry > at or g in completed}
    pending = {g: choices - occupied for g, choices in active.items() if g not in completed}
    original = optimum(active)
    attainable = len(bookings) + optimum(pending)
    return {
        "valid_bookings": valid,
        "unique_effects": unique,
        "completed": len(bookings),
        "initial_optimum_at_audit_time": original,
        "attainable_including_completed": attainable,
        "avoidable_loss": original - attainable,
        "bookings": bookings,
    }
