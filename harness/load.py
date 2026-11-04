"""Seeded goal arrivals, open-loop background clients and runner self-monitoring (6)."""

from __future__ import annotations

import asyncio
import random
import resource
import time
from urllib.parse import urlencode

import httpx

from data.generator.generate import DAYS, GRID_H, GRID_W, SERVICES, day
from harness.events import EventLog


def poisson_arrivals(rate_per_s: float, seed: int, start_s: float, stop_s: float) -> list[float]:
    """Arrival offsets (s from trial start) of a seeded Poisson process on [start_s, stop_s)."""
    rng = random.Random(seed)
    t, out = start_s, []
    while True:
        t += rng.expovariate(rate_per_s)
        if t >= stop_s:
            return out
        out.append(round(t, 3))


class RawPool:
    """Minimal HTTP/1.1 GET client with an O(1) keep-alive free list, for high-rate background traffic.

    httpx's pool scans all connections per request, which saturated the runner's event loop when a fault left
    hundreds of background requests in flight. A connection whose request timed out is closed, never reused.
    """

    def __init__(self, base_url: str):
        u = httpx.URL(base_url)
        self.host, self.port = u.host, u.port or 80
        self.free: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        self.open: set[asyncio.StreamWriter] = set()

    async def _connect(self):
        conn = await asyncio.open_connection(self.host, self.port)
        self.open.add(conn[1])
        return conn

    async def get(self, path: str, params: dict | None, headers: dict[str, str]) -> int:
        target = path + ("?" + urlencode(params) if params else "")
        head = (f"GET {target} HTTP/1.1\r\nHost: {self.host}\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode()
        conn = None
        while self.free:
            r, w = self.free.pop()
            if not w.is_closing():
                conn = (r, w)
                break
        reused = conn is not None
        if conn is None:
            conn = await self._connect()
        reader, writer = conn
        ok = False
        try:
            writer.write(head)
            first = await reader.readline()
            if not first and reused:
                # The server closed this idle keep-alive connection before reading the request (uvicorn's
                # keep-alive timeout). Like any HTTP client, send it on a fresh connection; nothing was processed.
                writer.close()
                self.open.discard(writer)
                conn = await self._connect()
                reader, writer = conn
                writer.write(head)
                first = await reader.readline()
            status = int(first.split()[1])
            length, close = 0, False
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b""):
                    break
                name, _, value = line.partition(b":")
                name = name.strip().lower()
                if name == b"content-length":
                    length = int(value)
                elif name == b"connection" and value.strip().lower() == b"close":
                    close = True
            await reader.readexactly(length)
            ok = not close
            return status
        finally:
            if ok:
                self.free.append(conn)
            else:
                writer.close()
                self.open.discard(writer)

    async def close(self) -> None:
        for w in list(self.open):
            w.close()
        self.open.clear()
        self.free.clear()


class Background:
    """Open-loop constant-rate read traffic: request i is sent at t0 + i / rate whatever happened before.
    70% single-day availability, 30% appointment reads; 2 s timeout, no retries, read-only."""

    def __init__(self, base_url: str, rate_rps: float, mix: dict[str, float], timeout_s: float, seed: int,
                 events: EventLog, n_appointments: int, trial_id: str):
        self.base_url = base_url.rstrip("/")
        self.rate = rate_rps
        self.p_day = mix.get("day_availability", 0.7)
        self.timeout_s = timeout_s
        self.rng = random.Random(seed)
        self.events = events
        self.n_appointments = n_appointments
        self.trial_id = trial_id
        self.regions = [f"R{r:02d}" for r in range(1, GRID_W * GRID_H + 1)]
        self.services = [code for code, _, _ in SERVICES]
        self.client = RawPool(base_url)
        self.sent = 0
        self.inflight: set[asyncio.Task] = set()

    def _request(self, i: int) -> tuple[str, str, dict | None]:
        if self.rng.random() < self.p_day:
            params = {"region": self.rng.choice(self.regions), "date": day(self.rng.randrange(DAYS)).isoformat(),
                      "service": self.rng.choice(self.services)}
            return "day_availability", "/availability", params
        return "get_appointment", f"/appointments/{self.rng.randint(1, self.n_appointments)}", None

    async def run(self, t0: float, start_s: float, stop_s: float) -> None:
        """Send requests on the fixed schedule from start_s to stop_s (offsets from monotonic t0)."""
        i = 0
        interval = 1.0 / self.rate
        while True:
            due = t0 + start_s + i * interval
            if due - t0 >= stop_s:
                break
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            kind, path, params = self._request(i)
            task = asyncio.create_task(self._one(i, kind, path, params))
            self.inflight.add(task)
            task.add_done_callback(self.inflight.discard)
            i += 1
        self.sent = i

    async def _one(self, i: int, kind: str, path: str, params: dict | None) -> None:
        op = f"bg_{i:07d}"
        headers = {"X-RB-Trial": self.trial_id, "X-RB-Op": op, "X-RB-Attempt": f"{op}_1", "X-RB-Actor": "background"}
        t0 = time.perf_counter()
        status, code = "ok", None
        try:
            async with asyncio.timeout(self.timeout_s):
                code = await self.client.get(path, params, headers)
            if not 200 <= code < 300:
                status = f"http_{code}"
        except TimeoutError:
            status = "timeout"
        except (OSError, ValueError, IndexError, asyncio.IncompleteReadError):
            status = "connection_error"
        self.events.emit(
            "http.attempt", actor_id="background", actor_kind="background", logical_op_id=op,
            attempt_id=f"{op}_1", attempt_n=1, retry_layer="none", tool=kind,
            route="GET /availability" if kind == "day_availability" else "GET /appointments/{appointment_id}",
            status=status, http_status=code, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            timeout_ms=self.timeout_s * 1000, stream="background",
        )

    async def close(self) -> None:
        for t in list(self.inflight):
            t.cancel()
        await asyncio.gather(*self.inflight, return_exceptions=True)
        await self.client.close()


class RunnerHealth:
    """Event-loop lag (a 100 ms ticker's lateness) and runner CPU per 1 s window, as `runner.sample` events."""

    def __init__(self, events: EventLog, tick_s: float = 0.1):
        self.events = events
        self.tick_s = tick_s
        self.lags_ms: list[float] = []
        self.cpu_pct: list[float] = []

    async def run(self) -> None:
        loop_t = time.monotonic()
        win_start, win_cpu = loop_t, _cpu_s()
        win_lags: list[float] = []
        while True:
            target = loop_t + self.tick_s
            await asyncio.sleep(max(0.0, target - time.monotonic()))
            now = time.monotonic()
            lag = max(0.0, (now - target) * 1000)
            win_lags.append(lag)
            self.lags_ms.append(lag)
            loop_t = target if now - target < 1.0 else now
            if now - win_start >= 1.0:
                cpu = 100 * (_cpu_s() - win_cpu) / (now - win_start)
                self.cpu_pct.append(cpu)
                self.events.emit("runner.sample", cpu_pct=round(cpu, 1), loop_lag_max_ms=round(max(win_lags), 2),
                                 loop_lag_p50_ms=round(sorted(win_lags)[len(win_lags) // 2], 2), stream="runner")
                win_start, win_cpu, win_lags = now, _cpu_s(), []


def _cpu_s() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime
