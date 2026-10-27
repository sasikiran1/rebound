"""Calibration step 1: background-only step test. Raise the open arrival rate until the knee
(p95 > 2x its floor, or errors > 1%). Capacity = the last rate before the knee, in req/s and DB-time/s.

Backend-side numbers only: latency is FieldDesk's `total_ms`, success is 2xx within the 2 s background timeout,
plus client-side timeouts and connection errors counted as errors.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

import httpx

from harness import dbadmin, settings, sut
from harness.events import EventLog
from harness.fault import Toxiproxy
from harness.load import Background, RunnerHealth
from harness.manifest import ROOT, load


def pct(values, q):
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))] if s else float("nan")


async def step_test(manifest_path: str, rates: list[float], step_s: float = 40, settle_s: float = 10) -> dict:
    m = load(manifest_path)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    run_dir = ROOT / "runs" / "calibration" / f"steptest_{stamp}"
    trial_id = f"steptest_{stamp}"
    events = EventLog(run_dir, experiment_id="calibration", trial_id=trial_id, condition="background", gate="off")
    async with httpx.AsyncClient(timeout=10) as c:
        await Toxiproxy(c).reset()
    fp = await dbadmin.clone_trial_db()
    await asyncio.to_thread(sut.restart_fielddesk, sut.fielddesk_env(m, gate_on=False))
    health = RunnerHealth(events)
    health_task = asyncio.create_task(health.run())
    steps = []
    t0 = time.monotonic()
    offset = 0.0
    try:
        for rate in rates:
            bg = Background(settings.FIELDDESK_URL, rate, m.background.mix, m.background.timeout_s,
                            m.background.seed + int(rate), events, fp["tables"]["appointments"]["rows"], trial_id)
            n_cpu0 = len(health.cpu_pct)
            await bg.run(t0, offset, offset + step_s)
            await asyncio.sleep(m.background.timeout_s + 0.5)
            await bg.close()
            steps.append({"rate_rps": rate, "start": offset, "end": offset + step_s,
                          "runner_cpu_p95": pct(health.cpu_pct[n_cpu0:], 0.95)})
            offset = time.monotonic() - t0 + 1.0
    finally:
        health_task.cancel()
        events.close()

    await asyncio.sleep(1.0)
    sut.copy_logs(run_dir)
    client = [json.loads(x) for x in (run_dir / "background.jsonl").read_text().splitlines()]
    backend = [json.loads(x) for x in (run_dir / "backend.jsonl").read_text().splitlines()
               if json.loads(x).get("trial_id") == trial_id]
    t0_ns = int(t0 * 1e9)
    for s in steps:
        a, b = s["start"] + settle_s, s["end"]
        be = [r for r in backend if a <= (r["ts_mono_ns"] - t0_ns) / 1e9 < b]
        cl = [e for e in client if a <= (e["ts_mono_ns"] - t0_ns) / 1e9 - (e["latency_ms"] or 0) / 1000 < b]
        lat = [r["total_ms"] for r in be]
        errors = sum(1 for e in cl if e["status"] != "ok")
        s.update({
            "backend_requests": len(be), "achieved_rps": len(be) / (b - a),
            "p50_ms": pct(lat, 0.5), "p95_ms": pct(lat, 0.95), "p99_ms": pct(lat, 0.99),
            "error_share": errors / len(cl) if cl else float("nan"),
            "db_ms_per_s": sum(r["db_ms"] for r in be) / (b - a),
            "db_ms_per_req": sum(r["db_ms"] for r in be) / len(be) if be else float("nan"),
        })
    floor = steps[0]["p95_ms"]
    knee = next((s for s in steps if s["p95_ms"] > 2 * floor or s["error_share"] > 0.01), None)
    before = [s for s in steps if knee is None or s["rate_rps"] < knee["rate_rps"]]
    cap = before[-1] if before else steps[0]
    out = {"run_dir": str(run_dir.relative_to(ROOT)), "steps": steps, "p95_floor_ms": floor,
           "knee_rate_rps": knee["rate_rps"] if knee else None,
           "capacity_rps": cap["achieved_rps"], "capacity_db_ms_per_s": cap["db_ms_per_s"],
           "capacity_step": cap["rate_rps"]}
    (run_dir / "steptest.json").write_text(json.dumps(out, indent=2) + "\n")
    for s in steps:
        print(f"rate {s['rate_rps']:6.0f}  achieved {s['achieved_rps']:7.1f}  p50 {s['p50_ms']:7.2f}  "
              f"p95 {s['p95_ms']:8.2f}  p99 {s['p99_ms']:8.2f}  err {s['error_share']:.3%}  "
              f"db_ms/s {s['db_ms_per_s']:8.1f}  runner cpu p95 {s['runner_cpu_p95']:.0f}%")
    print(json.dumps({k: v for k, v in out.items() if k != "steps"}, indent=2))
    return out


def run(manifest_path: str, rates: list[float], step_s: float) -> dict:
    import uvloop

    return uvloop.run(step_test(manifest_path, rates, step_s))
