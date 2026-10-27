"""asyncpg pool with per-request DB-time accounting.

Every database round trip (queries, BEGIN, COMMIT, ROLLBACK) goes through `TimedConnection`, which adds
its wall time to the current request's `RequestStats`. DB time therefore includes the network hop through
toxiproxy, which is exactly what a latency fault on the DB link inflates.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import asyncpg


@dataclass
class RequestStats:
    db_ms: float = 0.0
    queries: int = 0
    pool_wait_ms: float = 0.0


current_stats: ContextVar[RequestStats | None] = ContextVar("current_stats", default=None)


class TimedConnection:
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def _timed(self, fn, *args):
        t0 = time.perf_counter()
        try:
            return await fn(*args)
        finally:
            stats = current_stats.get()
            if stats is not None:
                stats.db_ms += (time.perf_counter() - t0) * 1000
                stats.queries += 1

    async def fetch(self, query: str, *args):
        return await self._timed(self._conn.fetch, query, *args)

    async def fetchrow(self, query: str, *args):
        return await self._timed(self._conn.fetchrow, query, *args)

    async def fetchval(self, query: str, *args):
        return await self._timed(self._conn.fetchval, query, *args)

    async def execute(self, query: str, *args):
        return await self._timed(self._conn.execute, query, *args)

    @asynccontextmanager
    async def transaction(self):
        tx = self._conn.transaction()
        await self._timed(tx.start)
        try:
            yield
        except BaseException:
            await self._timed(tx.rollback)
            raise
        else:
            await self._timed(tx.commit)


class Database:
    """Fixed-size pool. Connection settings come from the standard PG* environment variables."""

    def __init__(self, size: int):
        self.size = size
        self.pool: asyncpg.Pool | None = None
        self.in_use = 0
        self.waiters = 0

    async def open(self) -> None:
        self.pool = await asyncpg.create_pool(min_size=self.size, max_size=self.size)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    @asynccontextmanager
    async def acquire(self):
        stats = current_stats.get()
        t0 = time.perf_counter()
        self.waiters += 1
        try:
            conn = await self.pool.acquire()
        finally:
            self.waiters -= 1
        if stats is not None:
            stats.pool_wait_ms += (time.perf_counter() - t0) * 1000
        self.in_use += 1
        try:
            yield TimedConnection(conn)
        finally:
            self.in_use -= 1
            # Release runs asyncpg's connection reset, one more round trip the request waits on.
            t1 = time.perf_counter()
            try:
                await self.pool.release(conn)
            finally:
                if stats is not None:
                    stats.db_ms += (time.perf_counter() - t1) * 1000


db = Database(size=int(os.environ.get("POOL_SIZE", "8")))
