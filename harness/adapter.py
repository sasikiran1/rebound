"""Tool adapter between a client and FieldDesk ("Tool adapter").

- Host allowlist: requests go only to the manifest's FieldDesk base URL, built from fixed path templates.
- JSON Schema validation of every argument, then scope checks; failures return `invalid_arguments`.
  Scope: customer and appointment reads only for the goal's customer; the write only for the goal's appointment.
  Availability and the technician directory are public reads.
- At most one applied write per goal; the idempotency key is derived from the goal ID.
- 3.0 s deadline per attempt, no transport retries. Agents get exactly one attempt per operation, so every retry
  is the model's decision; only the scripted client passes a `RetryPolicy`, and its retries are labeled.
- Results truncated to 4 KB with a total count; errors use one shape.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jsonschema

from harness.events import EventLog, args_hash, attempt_id, op_id

HARNESS_ONLY_TOOLS = {"defer_goal"}
ROUTES = {
    "get_customer_appointments": ("GET", "/customers/{customer_id}/appointments"),
    "get_appointment": ("GET", "/appointments/{appointment_id}"),
    "check_day_availability": ("GET", "/availability"),
    "search_availability": ("GET", "/availability/search"),
    "list_technicians": ("GET", "/technicians"),
    "reschedule_appointment": ("POST", "/appointments/{appointment_id}/reschedule"),
}


def load_tools(path: str | Path = "prompts/tools_v1.json") -> list[dict]:
    return json.loads(Path(path).read_text())


def new_http_client() -> httpx.AsyncClient:
    """No transport retries, no client-side timeouts (the adapter enforces its own per-attempt deadline)."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0),
        timeout=None,
        limits=httpx.Limits(max_connections=400, max_keepalive_connections=200),
    )


@dataclass
class GoalScope:
    trial_id: str
    goal_n: int
    customer_id: int
    appointment_id: int
    allowed_regions: list[str]
    actor_kind: str = "agent"  # agent | scripted

    @property
    def goal_id(self) -> str:
        return f"g_{self.goal_n:04d}"

    @property
    def actor_id(self) -> str:
        return f"{self.actor_kind}_{self.goal_n:04d}"

    @property
    def idempotency_key(self) -> str:
        return f"{self.trial_id}:{self.goal_id}:reschedule"


@dataclass
class GoalState:
    ops: int = 0
    tool_calls: int = 0
    attempts: int = 0
    invalid_arguments: int = 0
    scope_violations: int = 0
    write_applied: bool = False
    change_id: int | None = None
    deferred_reason: str | None = None
    known_appointments: set[int] = field(default_factory=set)
    gate_rejections: int = 0
    non_rejected_attempts: int = 0
    hold_until: float | None = None  # retry_after_guard: monotonic time before which no attempt is sent
    retry_after_holds: int = 0
    retry_after_hold_s: float = 0.0


@dataclass
class RetryPolicy:
    """Scripted client only: up to `max_retries` per operation on timeout, 5xx or 429;
    full-jitter exponential backoff; honors Retry-After up to `retry_after_cap_s`."""

    max_retries: int | None = 2
    base_s: float = 0.5
    cap_s: float = 8.0
    retry_after_cap_s: float = 30.0
    rng: object = None

    # None: no fixed retry count; retries stop only at the request budget or the goal deadline (robust_v2).
    def exhausted(self, n: int) -> bool:
        return self.max_retries is not None and n > self.max_retries

    def retryable(self, status: str) -> bool:
        return status in ("timeout", "connection_error", "http_429") or (
            status.startswith("http_5") and status != "http_501"
        )

    def wait_s(self, retry_n: int, retry_after_s: float | None) -> float:
        if retry_after_s is not None:
            return min(retry_after_s, self.retry_after_cap_s)
        return self.rng.uniform(0, min(self.cap_s, self.base_s * 2 ** min(retry_n - 1, 30)))


def error(type_: str, message: str, retry_after_s: float | None = None) -> dict:
    err: dict = {"type": type_, "message": message}
    if retry_after_s is not None:
        err["retry_after_s"] = retry_after_s
    return {"error": err}


def truncate(result: dict, limit: int) -> dict:
    """Keep the result under `limit` bytes of JSON by trimming its largest list, adding counts."""
    if len(json.dumps(result)) <= limit:
        return result
    lists = [k for k, v in result.items() if isinstance(v, list)]
    if not lists:
        return {"truncated": True, "text": json.dumps(result)[: limit - 40]}
    key = max(lists, key=lambda k: len(json.dumps(result[k])))
    items = result[key]
    total = result.get("total_count", len(items))
    lo, hi = 0, len(items)
    while lo < hi:  # largest prefix that fits
        mid = (lo + hi + 1) // 2
        trial = {**result, key: items[:mid], "total_count": total, "returned_count": mid, "truncated": True}
        if len(json.dumps(trial)) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return {**result, key: items[:lo], "total_count": total, "returned_count": lo, "truncated": True}


def build_request(name: str, args: dict, scope: GoalScope) -> tuple[str, str, str, dict | None, dict | None]:
    """(method, route template, path, query params, JSON body) for one validated tool call."""
    method, template = ROUTES[name]
    path = template.format(**args)
    params, body = None, None
    if name == "check_day_availability":
        params = {"region": args["region"], "date": args["date"], "service": args["service"]}
    elif name == "search_availability":
        params = {"service": args["service"], "from": args["date_from"], "to": args["date_to"],
                  "regions": ",".join(args["regions"])}
    if name in ("check_day_availability", "search_availability"):
        # Tool set v2 only (prompts/tools_v2_*.json); v1 schemas reject these arguments before this point.
        params.update({k: args[k] for k in ("start_after", "start_before", "offset") if k in args})
    elif name == "list_technicians":
        params = {"region": args["region"], "skill": args["skill"]}
    elif name == "reschedule_appointment":
        body = {"slot_id": args["slot_id"], "customer_id": scope.customer_id}
    return method, template, path, params, body


class ToolAdapter:
    def __init__(self, base_url: str, client: httpx.AsyncClient, tools: list[dict], events: EventLog,
                 timeout_s: float = 3.0, max_result_bytes: int = 4096, allowed_hosts: tuple[str, ...] = (),
                 sleep=asyncio.sleep, retry_after_guard: bool = False, retry_after_guard_cap_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.allowed_hosts = allowed_hosts or (httpx.URL(self.base_url).host,)
        self.client = client
        self.schemas = {t["name"]: t["parameters"] for t in tools}
        self.events = events
        self.timeout_s = timeout_s
        self.max_result_bytes = max_result_bytes
        self.sleep = sleep
        # Runtime back-pressure guard (a treatment arm, off by default): after a 503/429 with Retry-After, the goal's
        # next attempt waits until that time has passed. It never sends a request the client did not ask for.
        self.retry_after_guard = retry_after_guard
        self.retry_after_guard_cap_s = retry_after_guard_cap_s

    def url(self, path: str) -> str:
        url = httpx.URL(self.base_url + path)
        if url.host not in self.allowed_hosts:
            raise ValueError(f"host {url.host} not allowed")
        return str(url)

    def check(self, scope: GoalScope, state: GoalState, name: str, args: dict) -> str | None:
        """Schema and scope checks. Returns an error message, or None if the call may proceed."""
        if name not in self.schemas:
            return f"unknown tool {name!r}"
        try:
            jsonschema.validate(args, self.schemas[name])
        except jsonschema.ValidationError as exc:
            return f"schema: {exc.message}"
        scope_msg = None
        if name == "get_customer_appointments" and args["customer_id"] != scope.customer_id:
            scope_msg = "out of scope: only the task's customer may be read"
        elif name == "get_appointment" and args["appointment_id"] not in (
            state.known_appointments | {scope.appointment_id}
        ):
            scope_msg = "out of scope: appointment does not belong to the task's customer"
        elif name == "reschedule_appointment":
            if args["appointment_id"] != scope.appointment_id:
                scope_msg = "out of scope: only the appointment described in the task may be changed"
            elif state.write_applied:
                scope_msg = "out of scope: this task's appointment has already been rescheduled"
        if scope_msg is not None:
            state.scope_violations += 1
        return scope_msg

    async def call(self, scope: GoalScope, state: GoalState, name: str, raw_args: str | dict,
                   model_call_id: str | None = None, retry: RetryPolicy | None = None,
                   deadline: float | None = None, attempt_budget: int | None = None) -> dict:
        """Run one tool call as one logical operation: one HTTP attempt, or more only under a RetryPolicy.
        `deadline` (monotonic) stops further retries once reached. `attempt_budget` caps the goal's total HTTP attempts,
        internal retries included; a call that would exceed it sends nothing and returns `budget_exhausted`."""
        state.ops += 1
        state.tool_calls += 1
        k = state.ops
        ids = {
            "goal_id": scope.goal_id, "actor_id": scope.actor_id, "actor_kind": scope.actor_kind,
            "model_call_id": model_call_id, "logical_op_id": op_id(scope.goal_n, k), "parent_id": model_call_id,
            "tool": name,
        }
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            args, problem = {}, f"arguments are not valid JSON: {exc}"
        else:
            problem = self.check(scope, state, name, args)
        ids["args_hash"] = args_hash(args)

        if problem is not None:
            state.invalid_arguments += 1
            self.events.emit("tool.op", **ids, status="invalid_arguments", latency_ms=0.0, attempts=0)
            return error("invalid_arguments", problem)

        if name == "defer_goal":
            state.deferred_reason = args["reason"]
            self.events.emit("tool.op", **ids, status="deferred", latency_ms=0.0, attempts=0, reason=args["reason"])
            return {"deferred": True}

        method, template, path, params, body = build_request(name, args, scope)
        url = self.url(path)
        t_op = time.perf_counter()
        n = 0
        if attempt_budget is not None and state.attempts >= attempt_budget:
            self.events.emit("tool.op", **ids, status="budget_exhausted", latency_ms=0.0, attempts=0)
            return error("budget_exhausted", f"request budget of {attempt_budget} used")
        while True:
            n += 1
            hold_ms = await self._hold(state)
            headers = {
                "X-RB-Trial": scope.trial_id, "X-RB-Goal": scope.goal_id, "X-RB-Op": ids["logical_op_id"],
                "X-RB-Attempt": attempt_id(scope.goal_n, k, n), "X-RB-Actor": scope.actor_id,
            }
            if body is not None:
                headers["Idempotency-Key"] = scope.idempotency_key
            result, attempt = await self.attempt(method, url, params, body, headers)
            state.attempts += 1
            if attempt["http_status"] == 503 and attempt["retry_after_s"] is not None:
                state.gate_rejections += 1
            else:
                state.non_rejected_attempts += 1
            if self.retry_after_guard and attempt["retry_after_s"] is not None and attempt["http_status"] in (429, 503):
                state.hold_until = time.monotonic() + min(attempt["retry_after_s"], self.retry_after_guard_cap_s)
            extra = {"retry_after_hold_ms": hold_ms} if self.retry_after_guard else {}
            self.events.emit(
                "http.attempt", **ids, route=f"{method} {template}", attempt_id=headers["X-RB-Attempt"],
                attempt_n=n, retry_layer="none" if n == 1 else "scripted", timeout_ms=self.timeout_s * 1000,
                request={"method": method, "path": path, "params": params, "body": body}, **attempt, **extra,
            )
            if attempt["status"] == "ok" or retry is None or retry.exhausted(n) or not retry.retryable(
                attempt["status"]
            ):
                break
            if attempt_budget is not None and state.attempts >= attempt_budget:
                break
            wait = retry.wait_s(n, attempt["retry_after_s"])
            if deadline is not None and time.monotonic() + wait >= deadline:
                break
            await self.sleep(wait)

        if attempt["status"] == "ok":
            if name == "get_customer_appointments":
                state.known_appointments |= {a["appointment_id"] for a in result.get("appointments", [])}
            if name == "reschedule_appointment":
                state.write_applied = True
                state.change_id = result.get("change_id")
            result = truncate(result, self.max_result_bytes)
        self.events.emit("tool.op", **ids, status=attempt["status"], http_status=attempt["http_status"],
                         latency_ms=round((time.perf_counter() - t_op) * 1000, 1), attempts=n)
        return result

    async def _hold(self, state: GoalState) -> float | None:
        """retry_after_guard only: wait out the goal's pending Retry-After before its next attempt; ms held."""
        if not self.retry_after_guard or state.hold_until is None:
            return None
        remaining = state.hold_until - time.monotonic()
        state.hold_until = None
        if remaining <= 0:
            return 0.0
        state.retry_after_holds += 1
        state.retry_after_hold_s += remaining
        await self.sleep(remaining)
        return round(remaining * 1000, 1)

    async def attempt(self, method, url, params, body, headers, timeout_s: float | None = None) -> tuple[dict, dict]:
        timeout_s = timeout_s or self.timeout_s
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                r = await self.client.request(method, url, params=params, json=body, headers=headers)
        except TimeoutError:
            latency = (time.perf_counter() - t0) * 1000
            return (error("timeout", f"no response within {timeout_s:.1f} s"),
                    {"status": "timeout", "http_status": None, "latency_ms": round(latency, 1), "retry_after_s": None})
        except httpx.HTTPError as exc:
            latency = (time.perf_counter() - t0) * 1000
            return (error("connection_error", type(exc).__name__),
                    {"status": "connection_error", "http_status": None, "latency_ms": round(latency, 1),
                     "retry_after_s": None})
        latency = round((time.perf_counter() - t0) * 1000, 1)
        retry_after = r.headers.get("retry-after")
        retry_after_s = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None
        meta = {"http_status": r.status_code, "latency_ms": latency, "retry_after_s": retry_after_s}
        if r.is_success:
            return r.json(), {"status": "ok", **meta}
        try:
            message = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = r.text[:200]
        status = f"http_{r.status_code}"
        return error(status, message, retry_after_s), {"status": status, **meta}
