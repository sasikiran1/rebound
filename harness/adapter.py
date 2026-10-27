"""Tool adapter between the model and FieldDesk ("Tool adapter").

- Host allowlist: requests go only to the manifest's FieldDesk base URL, built from fixed path templates.
- JSON Schema validation of every argument, then scope checks; failures return `invalid_arguments`.
- At most one applied write per goal; the idempotency key is derived from the goal ID.
- One attempt per operation, 3.0 s deadline, no transport retries. Every retry is the model's decision.
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
    "reschedule_appointment": ("POST", "/appointments/{appointment_id}/reschedule"),
}


def load_tools(path: str | Path = "prompts/tools_v1.json") -> list[dict]:
    return json.loads(Path(path).read_text())


def new_http_client() -> httpx.AsyncClient:
    """No transport retries, no client-side timeouts (the adapter enforces its own per-attempt deadline)."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0),
        timeout=None,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )


@dataclass
class GoalScope:
    trial_id: str
    goal_n: int
    customer_id: int
    appointment_id: int
    allowed_regions: list[str]

    @property
    def goal_id(self) -> str:
        return f"g_{self.goal_n:04d}"

    @property
    def actor_id(self) -> str:
        return f"agent_{self.goal_n:04d}"

    @property
    def idempotency_key(self) -> str:
        return f"{self.trial_id}:{self.goal_id}:reschedule"


@dataclass
class GoalState:
    ops: int = 0
    tool_calls: int = 0
    invalid_arguments: int = 0
    scope_violations: int = 0
    write_applied: bool = False
    change_id: int | None = None
    deferred_reason: str | None = None
    known_appointments: set[int] = field(default_factory=set)


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
    lo, hi = 0, len(items)
    while lo < hi:  # largest prefix that fits
        mid = (lo + hi + 1) // 2
        trial = {**result, key: items[:mid], "total_count": len(items), "returned_count": mid, "truncated": True}
        if len(json.dumps(trial)) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return {**result, key: items[:lo], "total_count": len(items), "returned_count": lo, "truncated": True}


class ToolAdapter:
    def __init__(self, base_url: str, client: httpx.AsyncClient, tools: list[dict], events: EventLog,
                 timeout_s: float = 3.0, max_result_bytes: int = 4096, allowed_hosts: tuple[str, ...] = ()):
        self.base_url = base_url.rstrip("/")
        self.allowed_hosts = allowed_hosts or (httpx.URL(self.base_url).host,)
        self.client = client
        self.schemas = {t["name"]: t["parameters"] for t in tools}
        self.events = events
        self.timeout_s = timeout_s
        self.max_result_bytes = max_result_bytes

    def _url(self, path: str) -> str:
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
        elif name == "check_day_availability" and args["region"] not in scope.allowed_regions:
            scope_msg = "out of scope: region is not the task's customer region"
        elif name == "reschedule_appointment":
            if args["appointment_id"] != scope.appointment_id:
                scope_msg = "out of scope: only the appointment described in the task may be changed"
            elif state.write_applied:
                scope_msg = "out of scope: this task's appointment has already been rescheduled"
        if scope_msg is not None:
            state.scope_violations += 1
        return scope_msg

    async def call(self, scope: GoalScope, state: GoalState, name: str, raw_args: str,
                   model_call_id: str | None = None) -> dict:
        """Run one model tool call as one logical operation with exactly one HTTP attempt."""
        state.ops += 1
        state.tool_calls += 1
        k = state.ops
        ids = {
            "goal_id": scope.goal_id, "actor_id": scope.actor_id, "actor_kind": "agent",
            "model_call_id": model_call_id, "logical_op_id": op_id(scope.goal_n, k), "parent_id": model_call_id,
            "tool": name,
        }
        try:
            args = json.loads(raw_args) if raw_args else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            args, problem = {}, f"arguments are not valid JSON: {exc}"
        else:
            problem = self.check(scope, state, name, args)
        ids["args_hash"] = args_hash(args)

        if problem is not None:
            state.invalid_arguments += 1
            self.events.emit("tool.op", **ids, status="invalid_arguments", latency_ms=0.0)
            return error("invalid_arguments", problem)

        if name == "defer_goal":
            state.deferred_reason = args["reason"]
            self.events.emit("tool.op", **ids, status="deferred", latency_ms=0.0)
            return {"deferred": True}

        method, template = ROUTES[name]
        path = template.format(**args)
        params, body, headers = None, None, {
            "X-RB-Trial": scope.trial_id, "X-RB-Goal": scope.goal_id, "X-RB-Op": ids["logical_op_id"],
            "X-RB-Attempt": attempt_id(scope.goal_n, k, 1), "X-RB-Actor": scope.actor_id,
        }
        if name == "check_day_availability":
            params = {"region": args["region"], "date": args["date"], "service": args["service"]}
        if name == "reschedule_appointment":
            body = {"slot_id": args["slot_id"], "customer_id": scope.customer_id}
            headers["Idempotency-Key"] = scope.idempotency_key

        result, attempt = await self._attempt(method, self._url(path), params, body, headers)
        self.events.emit(
            "http.attempt", **ids, route=f"{method} {template}", attempt_id=headers["X-RB-Attempt"], attempt_n=1,
            retry_layer="none", timeout_ms=self.timeout_s * 1000, **attempt,
        )
        if attempt["status"] == "ok":
            if name == "get_customer_appointments":
                state.known_appointments |= {a["appointment_id"] for a in result.get("appointments", [])}
            if name == "reschedule_appointment":
                state.write_applied = True
                state.change_id = result.get("change_id")
            result = truncate(result, self.max_result_bytes)
        self.events.emit("tool.op", **ids, status=attempt["status"], latency_ms=attempt["latency_ms"],
                         http_status=attempt["http_status"])
        return result

    async def _attempt(self, method, url, params, body, headers) -> tuple[dict, dict]:
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout_s):
                r = await self.client.request(method, url, params=params, json=body, headers=headers)
        except TimeoutError:
            latency = (time.perf_counter() - t0) * 1000
            return (error("timeout", f"no response within {self.timeout_s:.1f} s"),
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
