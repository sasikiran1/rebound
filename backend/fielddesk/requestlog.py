"""Pure-ASGI middleware: one `backend.request` JSONL line per request.

It is the outermost middleware, so `total_ms` covers the gate queue and the handler. It echoes the X-RB-*
identity headers for joining with client events. Nothing here reads them for any decision.
"""

from __future__ import annotations

import datetime as dt
import json
import time

from fielddesk.db import RequestStats, current_stats

ECHOED = {
    b"x-rb-trial": "trial_id",
    b"x-rb-goal": "goal_id",
    b"x-rb-op": "logical_op_id",
    b"x-rb-attempt": "attempt_id",
    b"x-rb-actor": "actor_id",
}
UNLOGGED_PATHS = {"/healthz"}


class Counters:
    def __init__(self):
        self.in_flight = 0
        self.requests_total = 0


counters = Counters()


class RequestLogMiddleware:
    def __init__(self, app, path: str | None):
        self.app = app
        self.out = open(path, "a", buffering=1) if path else None  # noqa: SIM115 - lives as long as the app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in UNLOGGED_PATHS:
            await self.app(scope, receive, send)
            return
        stats = RequestStats()
        token = current_stats.set(stats)
        ts_wall = dt.datetime.now(dt.UTC)
        ts_mono_ns = time.monotonic_ns()
        t0 = time.perf_counter()
        status: dict[str, int | None] = {"code": None}
        counters.in_flight += 1
        counters.requests_total += 1

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            counters.in_flight -= 1
            current_stats.reset(token)
            if self.out is not None:
                total_ms = (time.perf_counter() - t0) * 1000
                route = scope.get("route")
                record = {
                    "event_type": "backend.request",
                    "ts_wall": ts_wall.isoformat().replace("+00:00", "Z"),
                    "ts_mono_ns": ts_mono_ns,
                    **{field: None for field in ECHOED.values()},
                    **{ECHOED[k]: v.decode() for k, v in scope["headers"] if k in ECHOED},
                    "method": scope["method"],
                    "route": f"{scope['method']} {getattr(route, 'path', scope['path'])}",
                    "path": scope["path"],
                    "http_status": status["code"] or 500,
                    "gate_decision": stats.gate_decision,
                    "reject_reason": stats.reject_reason,
                    "queue_wait_ms": round(stats.queue_wait_ms, 3),
                    "total_ms": round(total_ms, 3),
                    "handler_ms": round(total_ms - stats.queue_wait_ms, 3),
                    "db_ms": round(stats.db_ms, 3),
                    "query_count": stats.queries,
                    "pool_wait_ms": round(stats.pool_wait_ms, 3),
                }
                self.out.write(json.dumps(record) + "\n")
