"""Frozen, local-only goal-preservation feasibility cohort. No provider fallback or retries."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import itertools
import json
import random
import sqlite3
import statistics
import time
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import HTTPException
from fielddesk.goal_preservation import Goal, Store, create_app

from harness.ollama import OllamaClient, Relay
from oracle.goal_preservation import inspect

MODELS = {
    "llama3.1:8b": "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e",
    "qwen3.5:4b": "d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04",
}
FIXTURES = {
    "avoidable_contention": {"alice": [9, 15], "bob": [15]},
    "no_contention": {"alice": [9], "bob": [15]},
    "insufficient_capacity": {"alice": [15], "bob": [15]},
}
SEEDS = [101, 102]
SYSTEM = (
    "You are a booking agent for ONE customer. Use tools to complete the customer's booking. "
    "Read available choices before acting. Only book an acceptable slot. "
    "Interpret actual tool results; a failed request is not a successful booking. "
    "If you cannot complete the booking, finish with an accurate explanation. "
    "Slots 9 and 15 mean 09:00 and 15:00 on the same requested day. "
    "You cannot act for other customers. Do not invent tool results."
)


def tool(name, description, properties):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool("list_slots", "Read this customer's available acceptable slots and current state version.", {}),
    tool(
        "evaluate",
        "Evaluate a proposed slot and read the server's response before committing.",
        {"slot_id": {"type": "integer"}},
    ),
    tool(
        "commit",
        "Attempt one booking using the current version returned by list_slots or evaluate.",
        {"slot_id": {"type": "integer"}, "version": {"type": "integer"}},
    ),
    tool("finish", "End this customer's session and report what happened.", {"message": {"type": "string"}}),
]


class OrdinaryStore(Store):
    """Ablation: same evaluator runs internally; hide collective data and remove only its enforcement."""

    def _evaluate(self, conn, goal_id, slot_id):
        r = super()._evaluate(conn, goal_id, slot_id)
        return {k: r[k] for k in ("goal_id", "slot_id", "version", "valid_until")} | {"allowed": True}


def list_slots(store, goal):
    with store.transaction() as conn:
        row = conn.execute("SELECT allowed,expires FROM goals WHERE id=?", (goal,)).fetchone()
        occupied = {r[0] for r in conn.execute("SELECT slot_id FROM bookings")}
        return {
            "goal_id": goal,
            "available_slots": [s for s in json.loads(row[0]) if s not in occupied],
            "version": conn.execute("SELECT n FROM revision").fetchone()[0],
        }


def prompt(choices):
    if choices == [9, 15]:
        return "Book my appointment. I prefer 15:00, but 09:00 is also acceptable. Book exactly one."
    return f"Book my appointment at {choices[0]:02d}:00 only. No other time is acceptable. Book exactly one."


async def session(app, store, model, goal, choices, seed, directory):
    directory.mkdir()
    client = OllamaClient(
        {"temperature": 0.3, "seed": seed, "num_ctx": 4096}, think=False, keep_alive="5m", raw_dir=directory / "model"
    )
    items = [{"role": "user", "content": prompt(choices)}]
    trace = []
    latencies = []
    out = {
        "goal_id": goal,
        "end": "call_budget",
        "model_calls": 0,
        "tool_calls": 0,
        "constraint_errors": 0,
        "recovered_after_error": False,
    }
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://research") as http:
            for call in range(1, 7):
                out["model_calls"] = call
                t = time.monotonic()
                response = await asyncio.wait_for(
                    client.responses.create(
                        model=model, instructions=SYSTEM, input=items, tools=TOOLS, max_output_tokens=384
                    ),
                    timeout=max(0.01, 90 - (time.monotonic() - start)),
                )
                latencies.append(time.monotonic() - t)
                actions = []
                for item in response.output:
                    if item.type == "message":
                        text = "".join(c.text for c in item.content)
                        items.append({"role": "assistant", "content": text})
                    elif item.type == "function_call":
                        actions.append(item)
                if not actions:
                    out["end"] = "protocol_error" if response.dropped_output else "no_action"
                    break
                for action in actions:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": action.call_id,
                            "name": action.name,
                            "arguments": action.arguments,
                        }
                    )
                stop = False
                for action in actions:
                    if out["tool_calls"] >= 8:
                        out["end"] = "tool_budget"
                        stop = True
                        result = {"error": "tool budget exhausted"}
                    else:
                        out["tool_calls"] += 1
                        try:
                            args = json.loads(action.arguments)
                            if action.name == "finish":
                                if set(args) != {"message"} or not isinstance(args["message"], str):
                                    raise ValueError("invalid finish arguments")
                                result = {"finished": True, "message": args["message"]}
                                out["end"] = "finished"
                                stop = True
                            elif action.name == "list_slots" and not args:
                                result = list_slots(store, goal)
                            elif action.name == "evaluate" and set(args) == {"slot_id"}:
                                if type(args["slot_id"]) is not int:
                                    raise ValueError("slot_id must be integer")
                                r = await http.get(f"/goals/{goal}/proposal", params=args)
                                result = {"status": r.status_code, "body": r.json()}
                            elif action.name == "commit" and set(args) == {"slot_id", "version"}:
                                if any(type(v) is not int for v in args.values()):
                                    raise ValueError("commit fields must be integers")
                                r = await http.post(f"/goals/{goal}/commit", json=args)
                                result = {"status": r.status_code, "body": r.json()}
                                if r.status_code >= 400:
                                    out["constraint_errors"] += 1
                                elif out["constraint_errors"]:
                                    out["recovered_after_error"] = True
                            else:
                                raise ValueError("unknown tool or invalid arguments")
                        except (ValueError, TypeError, HTTPException) as exc:
                            result = {"error": str(exc)}
                    trace.append({"call": call, "tool": action.name, "arguments": action.arguments, "result": result})
                    items.append(
                        {"type": "function_call_output", "call_id": action.call_id, "output": json.dumps(result)}
                    )
                    if stop:
                        break
                if stop:
                    break
    except TimeoutError:
        out["end"] = "timeout"
    except Exception as exc:
        out["end"] = "runner_or_provider_error"
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()
    out.update(wall_seconds=time.monotonic() - start, decision_seconds=latencies)
    with sqlite3.connect(store.path) as conn:
        booking = conn.execute("SELECT slot_id FROM bookings WHERE goal_id=?", (goal,)).fetchone()
    out["sql_completed"] = booking is not None
    out["final_state"] = (
        "correct"
        if booking and booking[0] in choices
        else "incorrect"
        if booking
        else "protocol_error"
        if out["end"] == "protocol_error"
        else "incomplete"
    )
    (directory / "session.json").write_text(json.dumps({"outcome": out, "trace": trace, "history": items}, indent=2))
    return out


def summarize(output):
    rows = [json.loads(p.read_text()) for p in sorted(output.glob("batch_*/result.json"))]
    cells = defaultdict(list)
    for r in rows:
        cells[(r["model"], r["arm"])].append(r)
    report = []
    for (model, arm), values in sorted(cells.items()):
        sessions = [s for v in values for s in v["sessions"]]
        times = [s["wall_seconds"] for s in sessions]
        latencies = [t for s in sessions for t in s["decision_seconds"]]
        report.append(
            {
                "model": model,
                "arm": arm,
                "batches": len(values),
                "sessions": len(sessions),
                "completed": sum(v["oracle"]["completed"] for v in values),
                "attainable_total": sum(v["oracle"]["initial_optimum_at_audit_time"] for v in values),
                "avoidable_loss": sum(v["oracle"]["avoidable_loss"] for v in values),
                "unfinished_or_error_sessions": sum(not s["sql_completed"] for s in sessions),
                "recovered_sessions": sum(s["recovered_after_error"] for s in sessions),
                "wall_median_range_s": [statistics.median(times), min(times), max(times)],
                "decision_median_range_s": [statistics.median(latencies), min(latencies), max(latencies)]
                if latencies
                else None,
            }
        )
    pairs = []
    indexed = {(r["model"], r["fixture"], r["order"], r["seed"], r["arm"]): r for r in rows}
    for r in rows:
        if r["arm"] != "ordinary":
            continue
        other = indexed.get((r["model"], r["fixture"], r["order"], r["seed"], "preservation"))
        if other:
            pairs.append(
                {
                    "model": r["model"],
                    "fixture": r["fixture"],
                    "order": r["order"],
                    "seed": r["seed"],
                    "completion_difference": other["oracle"]["completed"] - r["oracle"]["completed"],
                }
            )
    (output / "summary.json").write_text(json.dumps({"cells": report, "paired_batches": pairs}, indent=2))
    if report:
        with (output / "comparison.csv").open("w") as f:
            writer = csv.DictWriter(f, fieldnames=list(report[0]))
            writer.writeheader()
            writer.writerows(report)
    return report


def archive_sources(files, output, repo):
    paths = {}
    for source in files:
        relative = source.resolve().relative_to(repo.resolve())
        target = output / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        paths[str(source)] = str(target.relative_to(output))
    return paths


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    relay = Relay()
    try:
        metadata = await relay.post("/api/tags", None)
    finally:
        await relay.close()
    installed = {r["name"]: r["digest"] for r in metadata.get("body", {}).get("models", [])}
    if not metadata.get("ok") or any(installed.get(m) != digest for m, digest in MODELS.items()):
        raise RuntimeError("local model metadata mismatch; no inference started")
    protocol = Path("docs/goal_preservation_live_protocol.md").read_bytes()
    files = [
        Path(__file__),
        Path("backend/fielddesk/goal_preservation.py"),
        Path("oracle/goal_preservation.py"),
        Path("harness/ollama.py"),
        Path("scripts/local_models/ollama_relay_win.py"),
    ]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    archive_paths = archive_sources(files, output, Path.cwd())
    plan = []
    for model in MODELS:
        cells = list(itertools.product(FIXTURES, ("alice_first", "bob_first"), SEEDS, ("ordinary", "preservation")))
        random.Random(20261102).shuffle(cells)
        plan.extend({"model": model, "fixture": f, "order": o, "seed": s, "arm": a} for f, o, s, a in cells)
    (output / "protocol.md").write_bytes(protocol)
    (output / "freeze.json").write_text(
        json.dumps(
            {
                "frozen_before_inference_unix": time.time(),
                "models": MODELS,
                "source_hashes": hashes,
                "archive_paths": archive_paths,
                "protocol_sha256": hashlib.sha256(protocol).hexdigest(),
                "system": SYSTEM,
                "tools": TOOLS,
                "plan": plan,
                "kind": "exploratory_feasibility_not_confirmatory",
            },
            indent=2,
        )
    )
    # Ordinary deterministic scheduler reference, complete allocation known from the frozen fixture.
    reference = {}
    for name, goals in FIXTURES.items():
        path = output / f"scheduler_{name}.sqlite"
        store = Store(path)
        store.seed_slots([9, 15])
        at = time.time()
        for g, slots in goals.items():
            store.register(Goal(goal_id=g, acceptable_slots=slots, expires_at=at + 86400))
        used = set()
        with sqlite3.connect(path) as conn:
            for g in sorted(goals, key=lambda g: len(goals[g])):
                available = [s for s in goals[g] if s not in used]
                if available:
                    conn.execute("INSERT INTO bookings VALUES(?,?)", (g, available[0]))
                    used.add(available[0])
        reference[name] = inspect(path, at)
    (output / "scheduler.json").write_text(json.dumps(reference, indent=2))
    for n, cell in enumerate(plan, 1):
        directory = output / f"batch_{n:03d}"
        directory.mkdir()
        path = directory / "state.sqlite"
        app = create_app(path)
        if cell["arm"] == "ordinary":
            store = OrdinaryStore(path)
            # Route closures captured original Store; override only its evaluation method.
            app.state.store._evaluate = store._evaluate
        else:
            store = app.state.store
        store.seed_slots([9, 15])
        at = time.time()
        goals = FIXTURES[cell["fixture"]]
        for g, slots in goals.items():
            store.register(Goal(goal_id=g, acceptable_slots=slots, expires_at=at + 86400))
        order = ["alice", "bob"] if cell["order"] == "alice_first" else ["bob", "alice"]
        outcomes = [await session(app, store, cell["model"], g, goals[g], cell["seed"], directory / g) for g in order]
        result = {**cell, "sessions": outcomes, "oracle": inspect(path, at)}
        (directory / "result.json").write_text(json.dumps(result, indent=2))
        summarize(output)
        print(
            f"{n}/{len(plan)} {cell['model']} {cell['fixture']} {cell['arm']} "
            f"completed={result['oracle']['completed']} ends={[s['end'] for s in outcomes]}",
            flush=True,
        )
    (output / "complete.json").write_text(json.dumps({"batches": len(plan), "finished_unix": time.time()}))
    (output / "source_check.json").write_text(
        json.dumps({str(p): hashlib.sha256(p.read_bytes()).hexdigest() == hashes[str(p)] for p in files}, indent=2)
    )
    print(json.dumps(summarize(output), indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output))
