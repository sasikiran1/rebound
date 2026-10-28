"""Independent success check ("Oracle").

`gather_facts` reads final DB state directly through the read-only role (bypassing toxiproxy).
`classify` is a pure function of the task, those facts and how the harness says the goal ended.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import asyncpg

VERDICTS = (
    "correct", "incorrect", "duplicate_effect", "wrongly_deferred", "failed", "unfinished", "rejected",
    "provider_failed",
)


@dataclass
class Facts:
    changes: int  # applied changes on the goal's appointment
    other_customer_changes: int  # applied changes on the customer's other appointments
    status: str | None = None  # final state of the goal's appointment
    slot_id: int | None = None
    slot_date: dt.date | None = None
    service: str | None = None
    has_skill: bool | None = None
    covers_region: bool | None = None
    slot_occupants: int | None = None  # scheduled appointments in the final slot
    last_change_to_slot: int | None = None
    # appointment_changes.applied_at: clock_timestamp() of the INSERT, which precedes COMMIT (by one round trip,
    # about 400 ms during the fault). It is not the commit time; the backend logs commit acknowledgment separately.
    applied_at: dt.datetime | None = None


async def gather_facts(conn: asyncpg.Connection, task: dict) -> Facts:
    changes = await conn.fetch(
        "SELECT id, to_slot_id, applied_at FROM appointment_changes WHERE appointment_id = $1 ORDER BY id",
        task["appointment_id"],
    )
    other = await conn.fetchval(
        """
        SELECT count(*) FROM appointment_changes ch
        JOIN appointments a ON a.id = ch.appointment_id
        WHERE a.customer_id = $1 AND a.id <> $2
        """,
        task["customer_id"], task["appointment_id"],
    )
    row = await conn.fetchrow(
        """
        SELECT a.status, a.slot_id, s.slot_date, st.code AS service,
               EXISTS (SELECT 1 FROM technician_skills ts
                       WHERE ts.technician_id = s.technician_id AND ts.skill_id = st.skill_id) AS has_skill,
               EXISTS (SELECT 1 FROM technician_regions tr
                       WHERE tr.technician_id = s.technician_id AND tr.region_id = c.region_id) AS covers_region,
               (SELECT count(*) FROM appointments o
                WHERE o.slot_id = a.slot_id AND o.status = 'scheduled') AS slot_occupants
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        JOIN service_types st ON st.id = a.service_type_id
        JOIN customers c ON c.id = a.customer_id
        WHERE a.id = $1
        """,
        task["appointment_id"],
    )
    facts = Facts(changes=len(changes), other_customer_changes=other)
    if row is not None:
        facts.status, facts.slot_id, facts.slot_date = row["status"], row["slot_id"], row["slot_date"]
        facts.service, facts.has_skill, facts.covers_region = row["service"], row["has_skill"], row["covers_region"]
        facts.slot_occupants = row["slot_occupants"]
    if changes:
        facts.last_change_to_slot = changes[-1]["to_slot_id"]
        facts.applied_at = changes[-1]["applied_at"]
    return facts


def _effect_is_valid(task: dict, f: Facts) -> bool:
    return (
        f.status == "scheduled"
        and f.slot_date is not None
        and dt.date.fromisoformat(task["d1"]) <= f.slot_date <= dt.date.fromisoformat(task["d2"])
        and f.service == task["service"]
        and bool(f.has_skill)
        and bool(f.covers_region)
        and f.slot_occupants == 1
        and f.last_change_to_slot == f.slot_id
    )


def _no_effect_verdict(end_reason: str | None) -> str:
    return {
        "unfinished": "unfinished",
        "rejected": "rejected",
        "provider_failed": "provider_failed",
    }.get(end_reason or "", "failed")


def classify(task: dict, facts: Facts, end_reason: str | None) -> str:
    """end_reason comes from the harness: rescheduled | deferred | gave_up | limit_reached | provider_failed |
    unfinished | rejected (every request refused by the gate)."""
    if facts.changes > 1:
        return "duplicate_effect"
    if facts.other_customer_changes > 0:
        return "incorrect"
    if task["expected"] == "defer":
        if facts.changes > 0:
            return "incorrect"
        return "correct" if end_reason == "deferred" else _no_effect_verdict(end_reason)
    if facts.changes == 1:
        return "correct" if _effect_is_valid(task, facts) else "incorrect"
    if end_reason == "deferred":
        return "wrongly_deferred"
    return _no_effect_verdict(end_reason)


async def verdict_for(conn: asyncpg.Connection, task: dict, end_reason: str | None) -> dict:
    facts = await gather_facts(conn, task)
    return {
        "task_id": task["task_id"],
        "verdict": classify(task, facts, end_reason),
        "end_reason": end_reason,
        "change_inserted_at": facts.applied_at.isoformat() if facts.applied_at else None,
        "facts": {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in asdict(facts).items()},
    }
