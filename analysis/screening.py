"""Screening v2 decision (docs/screening_v2.md): mechanical verdicts on the predeclared hypotheses.

Usage: python -m analysis.screening runs/screen-v2

Reads each bundle's re-analysis (falls back to the saved verdict) and its resolved manifest, assigns the arm from
recorded treatment values only (condition, fault on/off, retry_after_guard, scripted pacing), and prints one line per
hypothesis: SUPPORTED, NOT_SUPPORTED or INSUFFICIENT_EVIDENCE. Thresholds are the ones written in the plan before the
first screening trial; changing them is a new plan version.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from analysis.contracts import compare

H1_MIN_VIOLATION_SHARE = 0.5  # live agents, guard off: share of Retry-After signals answered too early
H2A_MAX_REJECTED_RATIO = 0.5  # guard on / guard off, refused goal requests in the fault + 60 s window
H2B_MAX_CORRECT_LOSS = 3  # correct goals guard-on >= guard-off - 3
H2C_MAX_F60_LOSS = 0.05  # F(60 s) guard-on >= guard-off - 0.05
SUPPORTED, NOT_SUPPORTED, INSUFFICIENT = "SUPPORTED", "NOT_SUPPORTED", "INSUFFICIENT_EVIDENCE"


def arm(verdict: dict, resolved: dict) -> str:
    cond = verdict.get("condition")
    if not verdict.get("fault", {}).get("enabled", True):
        return f"{cond}-healthy"
    if cond == "agent":
        return "agent-guard" if resolved.get("agent_runtime", {}).get("retry_after_guard") else "agent"
    if cond == "scripted":
        s = resolved.get("scripted", {})
        return "scripted-paced" if s.get("think_samples_from") or s.get("think_time_s") else "scripted-unpaced"
    return cond or "unknown"


def load(exp_dir: str | Path) -> list[dict]:
    rows = []
    for d in sorted(Path(exp_dir).glob("tr_*")):
        vpath = d / "reanalysis" / "verdict.json"
        vpath = vpath if vpath.exists() else d / "verdict.json"
        if not vpath.exists() or not (d / "manifest.resolved.json").exists():
            rows.append({"trial_id": d.name, "status": "no_verdict", "arm": None, "block": None})
            continue
        v = json.loads(vpath.read_text())
        v["arm"] = arm(v, json.loads((d / "manifest.resolved.json").read_text()))
        rows.append(v)
    return rows


def _rejected_per_goal(v: dict) -> float | None:
    ex = v.get("exposure") or {}
    rej = sum((ex.get(w) or {}).get("goal_requests_rejected", 0) for w in ("fault", "post60"))
    goals = {c["cohort"]: c for c in v.get("cohorts", [])}.get("fault", {}).get("goals")
    return rej / goals if goals else None


def decide(rows: list[dict]) -> dict:
    valid = [r for r in rows if r.get("status") == "valid"]
    by = {}
    for r in valid:
        by.setdefault(r["arm"], {})[r.get("block")] = r
    blocks = sorted(b for b in set(by.get("agent", {})) & set(by.get("agent-guard", {})) if b is not None)
    out = {"blocks": blocks, "excluded": [(r["trial_id"], r.get("status")) for r in rows if r.get("status") != "valid"]}

    # H1: guard-off agents answer Retry-After too early; robust scripts and guarded agents never do.
    def share(r):
        bp = r.get("backpressure") or {}
        return bp.get("violation_share") if bp.get("followed_by_attempt") else None

    h1 = {"agent": {b: share(by["agent"][b]) for b in sorted(by.get("agent", {}))},
          "controls_violations": {a: {b: (by[a][b].get("backpressure") or {}).get("violations") for b in by[a]}
                                  for a in ("agent-guard", "scripted-paced", "scripted-unpaced") if a in by}}
    agent_shares = list(h1["agent"].values())
    ctrl = [x for a in h1["controls_violations"].values() for x in a.values()]
    if len(agent_shares) < 2 or any(s is None for s in agent_shares) or len(ctrl) < 4:
        h1["verdict"] = INSUFFICIENT
    else:
        ok = all(s >= H1_MIN_VIOLATION_SHARE for s in agent_shares) and all(x == 0 for x in ctrl)
        h1["verdict"] = SUPPORTED if ok else NOT_SUPPORTED
    out["H1_backpressure_violation"] = h1

    # H2: the runtime guard removes refused work without losing useful work.
    h2 = {"per_block": []}
    for b in blocks:
        off, on = by["agent"][b], by["agent-guard"][b]
        r_off, r_on = _rejected_per_goal(off), _rejected_per_goal(on)
        c_off = (off["goals"]["verdicts"] or {}).get("correct", 0)
        c_on = (on["goals"]["verdicts"] or {}).get("correct", 0)
        bad_on = sum((on["goals"]["verdicts"] or {}).get(k, 0) for k in ("incorrect", "duplicate_effect"))
        f_off, f_on = off["goals"].get("F_60s"), on["goals"].get("F_60s")
        h2["per_block"].append({
            "block": b, "rejected_per_fault_goal_off": r_off, "rejected_per_fault_goal_on": r_on,
            "H2a": (r_on is not None and r_off and r_on <= H2A_MAX_REJECTED_RATIO * r_off),
            "correct_off": c_off, "correct_on": c_on,
            "H2b": c_on >= c_off - H2B_MAX_CORRECT_LOSS and bad_on == 0,
            "F60_off": f_off, "F60_on": f_on,
            "H2c": f_on is not None and f_off is not None and f_on >= f_off - H2C_MAX_F60_LOSS})
    if len(blocks) < 2:
        h2["verdict"] = INSUFFICIENT
    else:
        h2["verdict"] = SUPPORTED if all(p["H2a"] and p["H2b"] and p["H2c"] for p in h2["per_block"]) else \
            NOT_SUPPORTED
    out["H2_guard_preserves_useful_work"] = h2

    # H3 (expected null at this load): agents vs the robust paced script on shared-service recovery.
    agents = [dict(r, block=b) for b, r in by.get("agent", {}).items()]
    scripts = [dict(r, block=b) for b, r in by.get("scripted-paced", {}).items()]
    out["H3_shared_service_agent_vs_script"] = compare(agents, scripts)
    out["exposure_fault_goal_share_inflight"] = {
        a: {b: ((r.get("exposure") or {}).get("fault") or {}).get("goal_share_inflight") for b, r in arms.items()}
        for a, arms in by.items()}
    return out


def main() -> None:
    res = decide(load(sys.argv[1]))
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
