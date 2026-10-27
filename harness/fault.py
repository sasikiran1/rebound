"""Toxiproxy control over its HTTP API (port 8474) and the 1 Hz canary query through the proxy."""

from __future__ import annotations

import time

import asyncpg
import httpx

from harness import settings


class Toxiproxy:
    def __init__(self, client: httpx.AsyncClient, base_url: str = settings.TOXIPROXY_URL):
        self.client = client
        self.base = base_url

    async def reset(self) -> None:
        """Remove all toxics and re-enable all proxies."""
        r = await self.client.post(f"{self.base}/reset")
        r.raise_for_status()

    async def add_latency(self, proxy: str, latency_ms: int, jitter_ms: int, name: str = "latency_fault") -> dict:
        toxic = {
            "name": name,
            "type": "latency",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"latency": latency_ms, "jitter": jitter_ms},
        }
        r = await self.client.post(f"{self.base}/proxies/{proxy}/toxics", json=toxic)
        r.raise_for_status()
        return r.json()

    async def remove(self, proxy: str, name: str = "latency_fault") -> None:
        r = await self.client.delete(f"{self.base}/proxies/{proxy}/toxics/{name}")
        r.raise_for_status()

    async def toxics(self, proxy: str) -> list[dict]:
        r = await self.client.get(f"{self.base}/proxies/{proxy}/toxics")
        r.raise_for_status()
        return r.json()


class Canary:
    """One persistent connection through the pg proxy; `probe()` times a trivial round trip."""

    def __init__(self):
        self.conn: asyncpg.Connection | None = None

    async def open(self) -> None:
        self.conn = await asyncpg.connect(settings.proxied_app_dsn())

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()

    async def probe(self) -> float:
        t0 = time.perf_counter()
        await self.conn.fetchval("SELECT 1")
        return (time.perf_counter() - t0) * 1000
