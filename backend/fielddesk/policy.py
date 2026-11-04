"""Agent policy file and refusal probe (docs/policy_probe_protocol.md). Both are off unless enabled by environment.

- AGENT_POLICY=on: FieldDesk serves /.well-known/agent-policy.json, and its 503 refusals state the policy in the
  error message. Off: refusals keep the gate's plain message.
- PROBE_REFUSE_FIRST=N: the first N requests of each goal (X-RB-Goal header) are refused with 503 and Retry-After,
  without touching the database. It makes refusals deterministic so agent behavior after a refusal can be measured
  without load or a fault.
"""

from __future__ import annotations

import json
import os
from collections import Counter

POLICY_PATH = "/.well-known/agent-policy.json"


def policy(retry_after_s: int) -> dict:
    return {
        "policy_version": 1,
        "applies_to": "automated clients and AI agents calling this API",
        "on_refusal": {
            "statuses": [429, 503],
            "rule": "send no request to this API until Retry-After seconds have passed",
            "how": "if you have a wait or sleep tool, call it for the Retry-After seconds before the next request",
            "default_retry_after_s": retry_after_s,
        },
        "expensive_operations": ["GET /availability/search"],
        "prefer": "GET /availability for one known date instead of a multi-day search",
        "give_up": "after 4 refusals for one task, stop and hand the task back instead of retrying",
    }


def refusal_message(retry_after_s: int, policy_on: bool) -> str:
    if not policy_on:
        return "server busy (probe), retry later"
    return (f"server busy, retry later. Policy for automated agents: send no request to this API for "
            f"{retry_after_s} seconds (Retry-After); if you have a wait tool, call it for {retry_after_s} seconds "
            f"first. Full policy: {POLICY_PATH}")


class PolicyMiddleware:
    def __init__(self, app, policy_on: bool | None = None, refuse_first: int | None = None, retry_after_s: int = 2):
        self.app = app
        self.policy_on = (os.environ.get("AGENT_POLICY", "off") == "on") if policy_on is None else policy_on
        self.refuse_first = int(os.environ.get("PROBE_REFUSE_FIRST", "0")) if refuse_first is None else refuse_first
        self.retry_after_s = retry_after_s
        self.seen: Counter = Counter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self.policy_on and scope["path"] == POLICY_PATH:
            await self._send(send, 200, policy(self.retry_after_s), [])
            return
        goal = dict(scope.get("headers") or []).get(b"x-rb-goal")
        if self.refuse_first and goal is not None:
            self.seen[goal] += 1
            if self.seen[goal] <= self.refuse_first:
                body = {"error": {"type": "overloaded", "message": refusal_message(self.retry_after_s, self.policy_on)}}
                await self._send(send, 503, body, [(b"retry-after", str(self.retry_after_s).encode())])
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _send(send, status: int, payload: dict, headers: list) -> None:
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()), *headers]})
        await send({"type": "http.response.body", "body": body})
