"""FieldDesk client for Resolve: one attempt per call, a per-attempt deadline, every call recorded.

`search_all` pages with `offset` until `total_count` is reached, so an eligible slot beyond the first page is never
hidden (the agent tool truncated results at 4 KB / 50 rows; the worker must see everything it decides over).
"""

from __future__ import annotations

import asyncio
import time

import httpx

PAGE = 50  # FieldDesk SEARCH_LIMIT


class ApiError(Exception):
    def __init__(self, status: str, http_status: int | None, message: str, body: dict | None = None,
                 retry_after: float | None = None):
        super().__init__(f"{status}: {message}")
        self.status, self.http_status, self.message, self.body = status, http_status, message, body or {}
        self.retry_after = retry_after  # seconds, from the server's Retry-After header

    @property
    def uncertain(self) -> bool:
        """The request may or may not have been applied (no definitive answer came back)."""
        return self.status in ("timeout", "connection_error") or (self.http_status or 0) >= 500


class FieldDesk:
    def __init__(self, base_url: str, timeout_s: float = 3.0, client: httpx.AsyncClient | None = None,
                 actor: str = "resolve"):
        self.base = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.client = client or httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0), timeout=None)
        self.actor = actor
        self.log: list[dict] = []

    async def close(self) -> None:
        await self.client.aclose()

    async def _call(self, op: str, method: str, path: str, params=None, body=None, headers=None) -> dict:
        t0 = time.perf_counter()
        entry = {"op": op, "method": method, "path": path, "params": params, "actor": self.actor}
        try:
            async with asyncio.timeout(self.timeout_s):
                r = await self.client.request(method, self.base + path, params=params, json=body,
                                              headers={"X-RB-Actor": self.actor, **(headers or {})})
        except TimeoutError as exc:
            entry.update(status="timeout", http_status=None, latency_ms=round((time.perf_counter() - t0) * 1e3, 1))
            self.log.append(entry)
            raise ApiError("timeout", None, f"no response within {self.timeout_s:.1f} s") from exc
        except httpx.HTTPError as exc:
            entry.update(status="connection_error", http_status=None,
                         latency_ms=round((time.perf_counter() - t0) * 1e3, 1))
            self.log.append(entry)
            raise ApiError("connection_error", None, type(exc).__name__) from exc
        entry.update(http_status=r.status_code, latency_ms=round((time.perf_counter() - t0) * 1e3, 1))
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"type": "bad_body", "message": r.text[:200]}}
        if r.is_success:
            entry["status"] = "ok"
            self.log.append(entry)
            return data
        err = data.get("error", {}) if isinstance(data, dict) else {}
        entry.update(status=f"http_{r.status_code}", error=err.get("type"))
        self.log.append(entry)
        try:
            retry_after = float(r.headers["Retry-After"]) if "Retry-After" in r.headers else None
        except ValueError:  # an HTTP-date form; treat as absent
            retry_after = None
        raise ApiError(f"http_{r.status_code}", r.status_code, err.get("message", ""), err, retry_after)

    async def context(self, customer_id: int) -> dict:
        return await self._call("context", "GET", f"/customers/{customer_id}/context")

    async def appointment(self, appointment_id: int) -> dict:
        return await self._call("get_appointment", "GET", f"/appointments/{appointment_id}")

    async def search_all(self, service: str, date_from: str, date_to: str, regions: list[str]) -> list[dict]:
        """Every open slot in the range, across pages. Raises if a page fails or the pages are inconsistent."""
        slots: list[dict] = []
        offset, total = 0, None
        while True:
            # start_after=00:00 selects the paged query (offset is only honoured on that path); it filters nothing.
            page = await self._call("search", "GET", "/availability/search", params={
                "service": service, "from": date_from, "to": date_to, "regions": ",".join(regions),
                "start_after": "00:00", "offset": offset})
            total = page["total_count"] if total is None else total
            slots += page["slots"]
            offset += len(page["slots"])
            if not page["slots"] or offset >= page["total_count"]:
                break
        seen = {s["slot_id"] for s in slots}
        if len(seen) != len(slots):
            raise ApiError("inconsistent_pages", None, "availability changed while paging")
        return slots

    async def reschedule(self, appointment_id: int, customer_id: int, slot_id: int, key: str) -> dict:
        return await self._call("reschedule", "POST", f"/appointments/{appointment_id}/reschedule",
                                body={"slot_id": slot_id, "customer_id": customer_id},
                                headers={"Idempotency-Key": key})

    async def void_key(self, key: str) -> dict:
        """Settle an uncertain write without repeating it: {'outcome': 'applied', 'change': …} if it committed,
        else {'outcome': 'voided'}, after which a late copy of that write is refused (409 key_voided)."""
        from urllib.parse import quote
        return await self._call("void_key", "POST", f"/idempotency/{quote(key, safe='')}/void")
