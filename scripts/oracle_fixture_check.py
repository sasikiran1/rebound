"""Oracle integration check against the real schema (no model): fresh clone, real FieldDesk writes, then verdict_for.

Usage: python scripts/oracle_fixture_check.py
Cases on task A0001 (narrow 1-day): (1) book qwen3-14b's 08:00 slot under an afternoon constraint -> incorrect;
(2) a 15:00 slot -> correct; (3) exact-slot race: a competitor books the slot, the loser defers -> correct;
(4) the same deferral while the slot is still free -> wrongly_deferred;
(5) A0001 without a constraint, 08:00 -> correct.
"""

import asyncio
import copy
import json
import sys

import asyncpg
import httpx

sys.path.insert(0, ".")
from harness import dbadmin, settings, sut  # noqa: E402
from harness.manifest import load  # noqa: E402
from oracle.verdicts import verdict_for  # noqa: E402

A0001 = json.loads(open("data/tasks/tasks_dev_A.jsonl").readline())


async def fresh():
    await dbadmin.clone_trial_db()
    await asyncio.to_thread(sut.restart_fielddesk, sut.fielddesk_env(load("manifests/frozen/eval_v1.yaml"), False))


async def book(http, appointment_id, customer_id, slot_id, key):
    r = await http.post(f"{settings.FIELDDESK_URL}/appointments/{appointment_id}/reschedule",
                        json={"slot_id": slot_id, "customer_id": customer_id}, headers={"Idempotency-Key": key})
    return r.status_code


async def score(task, end_reason):
    conn = await asyncpg.connect(settings.oracle_dsn())
    try:
        return (await verdict_for(conn, task, end_reason))["verdict"]
    finally:
        await conn.close()


async def main():
    late = {**A0001, "constraint": {"kind": "start_hour", "from_h": 15, "to_h": 16}}
    exact = {**A0001, "constraint": {"kind": "exact_slot", "slot_id": 6193}}
    results = []
    async with httpx.AsyncClient(timeout=10) as http:
        await fresh()
        results.append(("08:00 under afternoon constraint", await book(http, 34962, 11358, 6193, "k1"),
                        await score(late, "rescheduled"), "incorrect"))
        await fresh()
        results.append(("15:00 under afternoon constraint", await book(http, 34962, 11358, 6207, "k2"),
                        await score(late, "rescheduled"), "correct"))
        await fresh()
        results.append(("deferral while exact slot is still free", None, await score(exact, "deferred"),
                        "wrongly_deferred"))
        conn = await asyncpg.connect(settings.oracle_dsn())
        other = await conn.fetchrow(
            """SELECT a.id, a.customer_id FROM appointments a JOIN customers c ON c.id = a.customer_id
               JOIN regions r ON r.id = c.region_id JOIN service_types st ON st.id = a.service_type_id
               WHERE a.status = 'scheduled' AND r.code = 'R19' AND st.code = 'water_heater_install'
               AND a.customer_id <> 11358 ORDER BY a.id LIMIT 1""")
        await conn.close()
        competitor = await book(http, other["id"], other["customer_id"], 6193, "k3")
        results.append(("loser defers after competitor booked the exact slot", competitor,
                        await score(exact, "deferred"), "correct"))
        await fresh()
        results.append(("no constraint, 08:00 (frozen-eval semantics)", await book(http, 34962, 11358, 6193, "k4"),
                        await score(copy.deepcopy(A0001), "rescheduled"), "correct"))
    ok = True
    for name, http_status, got, want in results:
        ok &= got == want
        print(f"{'PASS' if got == want else 'FAIL'}  {name}: write HTTP {http_status}, verdict {got} (expected {want})")
    sys.exit(0 if ok else 1)


asyncio.run(main())
