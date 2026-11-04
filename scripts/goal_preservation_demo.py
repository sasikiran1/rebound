"""Constructed no-model demonstration; never modifies FieldDesk or historical trials."""

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import httpx
from fielddesk.goal_preservation import Goal, Store, create_app

from oracle.goal_preservation import inspect


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    fixture = {"alice": [15, 9], "bob": [15]}
    at = time.time()
    results = {}
    for mode in ("ordinary_first_choice", "centralized_constrained_first", "goal_preservation"):
        path = output / f"{mode}.sqlite"
        store = Store(path)
        store.seed_slots([9, 15])
        for goal, choices in fixture.items():
            store.register(Goal(goal_id=goal, acceptable_slots=choices, expires_at=at + 3600))
        trace = []
        if mode == "goal_preservation":
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(path)), base_url="http://research"
            ) as client:
                for goal in fixture:
                    chosen = fixture[goal][0]
                    r = await client.get(f"/goals/{goal}/proposal", params={"slot_id": chosen})
                    receipt = r.json()
                    trace.append({"event": "proposal", "receipt": receipt})
                    if not receipt["allowed"]:
                        rejected = await client.post(
                            f"/goals/{goal}/commit", json={"slot_id": chosen, "version": receipt["version"]}
                        )
                        trace.append(
                            {"event": "rejected_commit", "status": rejected.status_code, "body": rejected.json()}
                        )
                        chosen = receipt["alternative_slot_ids"][0]
                    committed = await client.post(
                        f"/goals/{goal}/commit", json={"slot_id": chosen, "version": receipt["version"]}
                    )
                    committed.raise_for_status()
                    trace.append({"event": "commit", "body": committed.json()})
        else:
            order = list(fixture) if mode == "ordinary_first_choice" else sorted(fixture, key=lambda g: len(fixture[g]))
            with sqlite3.connect(path) as conn:
                for goal in order:
                    occupied = {r[0] for r in conn.execute("SELECT slot_id FROM bookings")}
                    chosen = next((s for s in fixture[goal] if s not in occupied), None)
                    if chosen is not None:
                        conn.execute("INSERT INTO bookings VALUES(?,?)", (goal, chosen))
                    trace.append({"goal_id": goal, "slot_id": chosen})
        verdict = inspect(path, at)
        results[mode] = verdict
        (output / f"{mode}.json").write_text(json.dumps({"trace": trace, "oracle": verdict}, indent=2) + "\n")
    record = {
        "kind": "constructed_deterministic_demonstration",
        "live_model_sessions": 0,
        "transport": "ASGI HTTP requests in-process; real SQLite writes",
        "fixture": fixture,
        "results": results,
        "claim": "mechanics only; no novelty or agent-benefit conclusion",
    }
    (output / "summary.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory; existing paths refused")
    asyncio.run(run(parser.parse_args().output))
