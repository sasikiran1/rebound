"""Read FieldDesk's backend.jsonl request log."""

from __future__ import annotations

import json
from pathlib import Path

from harness import settings


def read(trial_id: str, path: str = settings.BACKEND_LOG) -> list[dict]:
    out = []
    with Path(path).open() as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("trial_id") == trial_id:
                out.append(rec)
    return out


def by_op(trial_id: str, path: str = settings.BACKEND_LOG) -> dict[str, dict]:
    return {r["logical_op_id"]: r for r in read(trial_id, path) if r.get("logical_op_id")}
