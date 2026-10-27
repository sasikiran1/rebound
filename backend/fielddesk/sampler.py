"""1 Hz `backend.sample` telemetry: in-flight, gate queue, pool use, CPU, memory.

Ticks are scheduled with `loop.call_at` on whole-second marks of the monotonic clock; `loop_lag_ms` is how
late each tick ran, a direct reading of event-loop saturation.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from pathlib import Path


def _rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


class Sampler:
    def __init__(self, path: str | None, counters, db, gate):
        self.out = open(path, "a", buffering=1) if path else None  # noqa: SIM115 - lives as long as the app
        self.counters, self.db, self.gate = counters, db, gate
        self.handle: asyncio.TimerHandle | None = None
        self.last_cpu = time.process_time()
        self.last_wall = time.monotonic()

    def start(self) -> None:
        if self.out is None:
            return
        loop = asyncio.get_running_loop()
        self._schedule(loop, int(loop.time()) + 1)

    def _schedule(self, loop, due: float) -> None:
        self.handle = loop.call_at(due, self._tick, loop, due)

    def _tick(self, loop, due: float) -> None:
        lag_ms = (loop.time() - due) * 1000
        now_cpu, now_wall = time.process_time(), time.monotonic()
        cpu_pct = 100 * (now_cpu - self.last_cpu) / max(1e-6, now_wall - self.last_wall)
        self.last_cpu, self.last_wall = now_cpu, now_wall
        record = {
            "event_type": "backend.sample",
            "ts_wall": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            "ts_mono_ns": time.monotonic_ns(),
            "in_flight": self.counters.in_flight,
            "requests_total": self.counters.requests_total,
            "gate_enabled": self.gate is not None,
            "gate_queue": self.gate.queue_len if self.gate else 0,
            "gate_tokens_free": self.gate.free if self.gate else None,
            "gate_rejected_total": self.gate.rejected if self.gate else 0,
            "pool_size": self.db.size,
            "pool_in_use": self.db.in_use,
            "pool_waiters": self.db.waiters,
            "cpu_pct": round(cpu_pct, 1),
            "rss_mb": _rss_mb(),
            "loop_lag_ms": round(lag_ms, 2),
        }
        self.out.write(json.dumps(record) + "\n")
        nxt = due + 1
        while nxt <= loop.time():  # skip ticks missed under heavy load instead of bunching them
            nxt += 1
        self._schedule(loop, nxt)

    def stop(self) -> None:
        if self.handle is not None:
            self.handle.cancel()
