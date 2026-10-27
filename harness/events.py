"""JSONL event log with the common envelope.

Every event carries the trial, goal, operation and attempt IDs (plus model call for agents); keys that do
not apply are present as null so all streams share one schema.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from pathlib import Path

ENVELOPE_KEYS = [
    "ts_wall", "ts_mono_ns", "experiment_id", "trial_id", "condition", "gate", "config_hash", "phase",
    "fault_state", "event_type", "goal_id", "actor_id", "actor_kind", "model_call_id", "logical_op_id",
    "attempt_id", "parent_id", "tool", "route", "args_hash", "attempt_n", "retry_layer", "status",
    "http_status", "latency_ms", "timeout_ms", "retry_after_s",
]

# Which file each event type goes to inside the trial directory.
STREAMS = {"model.call": "model_calls.jsonl", "oracle.verdict": "oracle.jsonl"}
DEFAULT_STREAM = "client.jsonl"


def args_hash(args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def goal_ids(goal_n: int) -> dict[str, str]:
    return {"goal_id": f"g_{goal_n:04d}", "actor_id": f"agent_{goal_n:04d}"}


def model_call_id(goal_n: int, k: int) -> str:
    return f"mc_{goal_n:04d}_{k:02d}"


def op_id(goal_n: int, k: int) -> str:
    return f"op_{goal_n:04d}_{k:02d}"


def attempt_id(goal_n: int, k: int, attempt: int) -> str:
    return f"at_{goal_n:04d}_{k:02d}_{attempt}"


class EventLog:
    def __init__(self, trial_dir: str | Path, **base):
        self.dir = Path(trial_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.base = base  # experiment_id, trial_id, condition, gate, config_hash
        self.context = {"phase": None, "fault_state": None}  # updated by the controller
        self._files: dict[str, object] = {}

    def emit(self, event_type: str, **fields) -> dict:
        record = {k: None for k in ENVELOPE_KEYS}
        record.update(self.base)
        record.update(self.context)
        record["ts_wall"] = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        record["ts_mono_ns"] = time.monotonic_ns()
        record["event_type"] = event_type
        record.update(fields)
        name = STREAMS.get(event_type, DEFAULT_STREAM)
        f = self._files.get(name)
        if f is None:
            f = self._files[name] = (self.dir / name).open("a", buffering=1)
        f.write(json.dumps(record, default=str) + "\n")
        return record

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()
