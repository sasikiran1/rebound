"""Manual measurements: the latency pulse (`make pulse`) and per-endpoint DB time (`make endpoints`)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
import statistics
import time
from pathlib import Path

import httpx

from harness import backendlog, settings
from harness.fault import Canary, Toxiproxy


def load_tasks(path: str = "data/tasks/tasks_dev_A.jsonl") -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


async def _api_get(client: httpx.AsyncClient, path: str, params: dict, trial: str, op: str) -> tuple[float, int]:
    headers = {"X-RB-Trial": trial, "X-RB-Op": op, "X-RB-Attempt": f"{op}_1", "X-RB-Actor": "probe"}
    t0 = time.perf_counter()
    r = await client.get(f"{settings.FIELDDESK_URL}{path}", params=params, headers=headers)
    return (time.perf_counter() - t0) * 1000, r.status_code


async def pulse(latency_ms: int = 400, jitter_ms: int = 50, before_s: int = 10, during_s: int = 10,
                after_s: int = 10) -> dict:
    trial = "pulse_" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    task = next(t for t in load_tasks() if t["kind"] == "narrow_1day")
    params = {"region": task["customer_region"], "date": task["d1"], "service": task["service"]}
    rows = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        tox = Toxiproxy(client)
        await tox.reset()
        canary = Canary()
        await canary.open()
        try:
            t_start = time.monotonic()
            total = before_s + during_s + after_s
            for i in range(total):
                if i == before_s:
                    await tox.add_latency("pg", latency_ms, jitter_ms)
                if i == before_s + during_s:
                    await tox.remove("pg")
                phase = "before" if i < before_s else ("during" if i < before_s + during_s else "after")
                canary_ms = await canary.probe()
                op = f"pulse_{i:03d}"
                api_ms, status = await _api_get(client, "/availability", params, trial, op)
                rows.append(
                    {"t": i, "phase": phase, "canary_ms": canary_ms, "api_ms": api_ms, "status": status, "op": op}
                )
                next_tick = t_start + i + 1
                await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        finally:
            await tox.reset()
            await canary.close()

    await asyncio.sleep(0.5)  # let FieldDesk flush its last log line
    backend = backendlog.by_op(trial)
    print(f"{'t':>3} {'phase':7} {'canary_ms':>10} {'api_ms':>9} {'db_ms':>9} {'queries':>7} {'status':>6}")
    for r in rows:
        b = backend.get(r["op"], {})
        r["db_ms"] = b.get("db_ms")
        r["queries"] = b.get("query_count")
        print(f"{r['t']:>3} {r['phase']:7} {r['canary_ms']:>10.1f} {r['api_ms']:>9.1f} "
              f"{(r['db_ms'] or float('nan')):>9.1f} {str(r['queries']):>7} {r['status']:>6}")

    summary = {"trial": trial, "latency_ms": latency_ms, "jitter_ms": jitter_ms}
    for phase in ("before", "during", "after"):
        sel = [r for r in rows if r["phase"] == phase]
        summary[phase] = {
            k: round(statistics.median(r[k] for r in sel if r[k] is not None), 1)
            for k in ("canary_ms", "api_ms", "db_ms")
        }
    rise = summary["during"]["canary_ms"] - summary["before"]["canary_ms"]
    summary["canary_rise_ms"] = round(rise, 1)
    summary["fault_verified"] = rise >= 0.7 * latency_ms
    print(json.dumps(summary, indent=2))
    return summary


async def endpoints(n: int = 100, seed: int = 1) -> dict:
    """Sequential requests per read endpoint on the healthy system; DB time from the backend log."""
    trial = "endpoints_" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    rng = random.Random(seed)
    tasks = load_tasks()
    plans = {
        "GET /customers/{customer_id}/appointments": lambda t: (f"/customers/{t['customer_id']}/appointments", {}),
        "GET /appointments/{appointment_id}": lambda t: (f"/appointments/{t['appointment_id']}", {}),
        "GET /availability": lambda t: (
            "/availability", {"region": t["customer_region"], "date": t["d1"], "service": t["service"]}
        ),
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        for route, plan in plans.items():
            for i in range(n):
                path, params = plan(rng.choice(tasks))
                await _api_get(client, path, params, trial, f"{route}#{i}")
    await asyncio.sleep(0.5)
    recs = backendlog.read(trial)
    out = {}
    for route in plans:
        sel = [r for r in recs if r["route"] == route and r["http_status"] == 200]
        db = [r["db_ms"] for r in sel]
        out[route] = {
            "n": len(sel),
            "db_ms_p50": round(pct(db, 0.5), 2),
            "db_ms_p95": round(pct(db, 0.95), 2),
            "handler_ms_p50": round(pct([r["handler_ms"] for r in sel], 0.5), 2),
            "queries": statistics.median(r["query_count"] for r in sel),
        }
    print(json.dumps(out, indent=2))
    return out
