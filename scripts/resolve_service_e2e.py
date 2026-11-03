"""End-to-end check of the deployed Resolve containers over HTTP (compose profile `resolve`).

    uv run python scripts/resolve_service_e2e.py [--case D2] [--service http://127.0.0.1:8091]

Seeds one dev fixture, opens a request through POST /api/requests (one model call in the service container), waits
for the worker container to ask for confirmation, answers "Yes, please book it." through the messages endpoint (a
plain yes needs no model call), waits for completion, and judges the database with the SQL oracle.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import settings  # noqa: E402
from resolve.eval import cases as C  # noqa: E402
from resolve.eval.oracle import verdict  # noqa: E402


async def wait_for(client, rid, states, limit_s=60):
    async with asyncio.timeout(limit_s):
        while True:
            r = (await client.get("/api/request", params={"rid": rid})).json()
            if r.get("state") in states:
                return r
            await asyncio.sleep(1)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="D2")
    ap.add_argument("--service", default="http://127.0.0.1:8091")
    a = ap.parse_args()
    case = next(c for c in C.load(C.CASE_DIR / "dev.yaml") if c.id == a.case)
    admin = await asyncpg.connect(settings.admin_dsn(settings.TRIAL_DB))
    oracle = await asyncpg.connect(settings.oracle_dsn())
    await C.seed(admin, case)
    times = []
    async with httpx.AsyncClient(base_url=a.service, timeout=60) as client:
        print("health", (await client.get("/healthz")).json())
        times.append(await oracle.fetchval("SELECT clock_timestamp()"))
        opened = (await client.post("/api/requests", json={
            "customer_id": case.customer_id, "appointment_id": case.appointment_id,
            "message": case.messages[0]})).json()
        rid = opened["request_id"]
        print("opened", json.dumps(opened))
        r = await wait_for(client, rid, {"needs_input", "completed", "unresolved"})
        print("worker container:", r["state"], (r.get("question") or {}).get("text"))
        if r["state"] == "needs_input" and r["question"]["kind"] == "confirm":
            times.append(await oracle.fetchval("SELECT clock_timestamp()"))
            print("reply", (await client.post(f"/api/requests/{rid}/messages",
                                              json={"text": "Yes, please book it."})).json())
            r = await wait_for(client, rid, {"completed", "unresolved"})
        print("final state", r["state"], "|", r["turns"][-1]["text"])
        print("metrics", (await client.get("/api/metrics")).json()["model_cost_usd_this_process"], "USD this process")
    print("oracle", await verdict(oracle, case, times))
    await admin.close()
    await oracle.close()


if __name__ == "__main__":
    asyncio.run(main())
