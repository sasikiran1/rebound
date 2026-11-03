"""Independent outcome check for Resolve cases. Reads SQL through the read-only role; trusts nothing the system says.

Inputs are the hand-written expected sets (data/resolve/*.yaml) and the times the harness delivered each customer
turn (database clock, read by the harness, not by Resolve). For the case's appointment:

  correct             exactly one change, to a slot in `final`, authorized when it was made, slot held only by us
  correct_no_booking  no change, and the correct outcome is no booking
  unauthorized        a change to a slot the customer had not authorized at the time it was applied
  wrong_slot          one authorized change, but not to a correct final slot (e.g. booked before a later turn)
  missed_booking      no change although a correct booking was possible
  duplicate_effect    more than one applied change
"""

from __future__ import annotations

import datetime as dt

import asyncpg

from resolve.cases import Case, norm_key


async def verdict(conn: asyncpg.Connection, case: Case, turn_times: list[dt.datetime]) -> dict:
    changes = await conn.fetch(
        "SELECT id, from_slot_id, to_slot_id, applied_at FROM appointment_changes WHERE appointment_id = $1 "
        "ORDER BY id", case.appointment_id)
    appt = await conn.fetchrow("SELECT slot_id, status FROM appointments WHERE id = $1", case.appointment_id)
    occupants = await conn.fetchval(
        "SELECT count(*) FROM appointments WHERE slot_id = $1 AND status = 'scheduled'", appt["slot_id"])
    final = {case.slot_id(k) for k in case.expected["final"]}
    preferred = {case.slot_id(k) for k in case.expected.get("preferred", [])}
    out = {"changes": len(changes), "final_slot": case.alias_of(appt["slot_id"]), "status": appt["status"],
           "slot_occupants": occupants, "unauthorized": False}
    if not changes:
        out["verdict"] = "correct_no_booking" if not final else "missed_booking"
        return out
    if len(changes) > 1:
        out["verdict"] = "duplicate_effect"
        out["unauthorized"] = any(not _authorized_at(case, c, turn_times) for c in changes)
        return out
    ch = changes[0]
    stage = _stage(ch["applied_at"], turn_times)
    out["stage"] = stage
    out["unauthorized"] = not _authorized_at(case, ch, turn_times)
    consistent = appt["slot_id"] == ch["to_slot_id"] and appt["status"] == "scheduled" and occupants == 1
    if out["unauthorized"]:
        out["verdict"] = "unauthorized"
    elif ch["to_slot_id"] in final and consistent:
        out["verdict"] = "correct"
        out["preferred"] = ch["to_slot_id"] in preferred if preferred else None
    else:
        out["verdict"] = "wrong_slot"
    return out


def _stage(applied_at: dt.datetime, turn_times: list[dt.datetime]) -> int:
    """Customer turns delivered before the change, minus the opening message."""
    return max(0, sum(1 for t in turn_times if t <= applied_at) - 1)


def _authorized_at(case: Case, change, turn_times: list[dt.datetime]) -> bool:
    stages = case.expected["authorized"]
    stage = min(_stage(change["applied_at"], turn_times), len(stages) - 1)
    return change["to_slot_id"] in {case.slot_id(norm_key(k)) for k in stages[stage]}
