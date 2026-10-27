"""Independent SQL check of a task file against a fresh trial DB (read-only role), written apart from the generator:
the appointment is the customer's scheduled appointment of that service on that date, and the number of open
qualifying slots in the window equals the recorded count (0 for infeasible tasks)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import asyncpg

from harness import settings

FREE_QUALIFYING = """
SELECT count(*) FROM slots s
JOIN technician_skills ts ON ts.technician_id = s.technician_id
JOIN service_types st ON st.skill_id = ts.skill_id AND st.code = $1
JOIN technician_regions tr ON tr.technician_id = s.technician_id
JOIN customers c ON c.region_id = tr.region_id AND c.id = $2
WHERE s.slot_date BETWEEN $3 AND $4
  AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.slot_id = s.id AND a.status = 'scheduled')
"""
APPOINTMENT = """
SELECT a.customer_id, a.status, st.code AS service, s.slot_date FROM appointments a
JOIN slots s ON s.id = a.slot_id JOIN service_types st ON st.id = a.service_type_id WHERE a.id = $1
"""


async def verify(task_file: str) -> list[str]:
    tasks = [json.loads(x) for x in Path(task_file).read_text().splitlines()]
    conn = await asyncpg.connect(settings.oracle_dsn())
    problems = []
    try:
        for t in tasks:
            a = await conn.fetchrow(APPOINTMENT, t["appointment_id"])
            if a is None or a["customer_id"] != t["customer_id"] or a["status"] != "scheduled" or (
                a["service"] != t["service"] or a["slot_date"].isoformat() != t["current_date"]
            ):
                problems.append(f"{t['task_id']}: appointment mismatch {dict(a) if a else None}")
                continue
            free = await conn.fetchval(FREE_QUALIFYING, t["service"], t["customer_id"],
                                       dt.date.fromisoformat(t["d1"]), dt.date.fromisoformat(t["d2"]))
            if free != t["free_candidates"] or (t["expected"] == "defer") != (free == 0):
                problems.append(f"{t['task_id']}: free {free} != recorded {t['free_candidates']} ({t['kind']})")
    finally:
        await conn.close()
    kinds = {k: sum(t["kind"] == k for t in tasks) for k in sorted({t["kind"] for t in tasks})}
    print(f"{task_file}: {len(tasks)} tasks {kinds}; {len(problems)} problems")
    for p in problems[:20]:
        print("  " + p)
    return problems
