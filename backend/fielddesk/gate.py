"""Admission gate (lean): one ASGI middleware before the handler and the DB pool.

- Concurrency limit of `tokens`; each request needs one token.
- Bounded FIFO queue of at most `queue_max` waiters. A request is rejected if the queue is full or it has
  waited `queue_wait_s`.
- Rejection: 503, `Retry-After`, a small JSON body, no DB access.

The gate decides from its own counters only (the path is read just to skip /healthz). It never reads identity
headers or anything else about the client.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque

from fielddesk.db import current_stats

UNGATED_PATHS = {"/healthz"}


class Gate:
    def __init__(self, tokens: int, queue_max: int, queue_wait_s: float):
        self.tokens = tokens
        self.free = tokens
        self.queue_max = queue_max
        self.queue_wait_s = queue_wait_s
        self.waiters: deque[asyncio.Future] = deque()
        self.admitted = 0
        self.queued = 0
        self.rejected = 0

    @property
    def queue_len(self) -> int:
        return sum(1 for f in self.waiters if not f.done())

    async def acquire(self) -> tuple[str, str | None]:
        """Returns (decision, reject_reason). decision is admitted | queued | rejected."""
        if self.free > 0 and self.queue_len == 0:
            self.free -= 1
            self.admitted += 1
            return "admitted", None
        if self.queue_len >= self.queue_max:
            self.rejected += 1
            return "rejected", "queue_full"
        fut = asyncio.get_running_loop().create_future()
        self.waiters.append(fut)
        try:
            async with asyncio.timeout(self.queue_wait_s):
                await fut
        except TimeoutError:
            self._discard(fut)
            if fut.done() and not fut.cancelled():  # token handed over just as the wait expired
                self.queued += 1
                return "queued", None
            self.rejected += 1
            return "rejected", "queue_timeout"
        except asyncio.CancelledError:
            self._discard(fut)
            if fut.done() and not fut.cancelled():
                self.release()
            raise
        self.queued += 1
        return "queued", None

    def _discard(self, fut: asyncio.Future) -> None:
        try:
            self.waiters.remove(fut)
        except ValueError:
            pass

    def release(self) -> None:
        """Hand the token to the oldest live waiter, or return it to the pool."""
        while self.waiters:
            fut = self.waiters.popleft()
            if not fut.done():
                fut.set_result(None)
                return
        self.free += 1


class GateMiddleware:
    def __init__(self, app, gate: Gate | None, retry_after_s: int = 2):
        self.app = app
        self.gate = gate
        self.retry_after_s = retry_after_s

    async def __call__(self, scope, receive, send):
        if self.gate is None or scope["type"] != "http" or scope["path"] in UNGATED_PATHS:
            await self.app(scope, receive, send)
            return
        stats = current_stats.get()
        t0 = time.perf_counter()
        decision, reason = await self.gate.acquire()
        if stats is not None:
            stats.gate_decision = decision
            stats.queue_wait_ms = (time.perf_counter() - t0) * 1000
            stats.reject_reason = reason
        if decision == "rejected":
            await self._reject(send, reason)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.release()

    async def _reject(self, send, reason: str | None) -> None:
        body = json.dumps({"error": {"type": "overloaded", "message": f"server busy ({reason}), retry later"}}).encode()
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(self.retry_after_s).encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
