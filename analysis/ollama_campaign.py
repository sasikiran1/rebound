"""Four-condition campaign table (fault x gate) with exposure, DB CPU, back-pressure and model-pacing measures.

Usage: python -m analysis.ollama_campaign <experiment_dir> [--out <csv>]

Reads each trial bundle (verdict.json from analysis.metrics plus the raw JSONL) and adds, per trial:
- ordinary-user service: background requests that succeeded (2xx within the 2 s client timeout) in the fault
  window [t_on, t_off) and in the first post-fault minute [t_off, t_off + 60), and offered requests there;
- goal vs background exposure in those windows: share of in-flight request-seconds (gate queue + handler, from the
  backend's total_ms) and share of DB round-trip milliseconds. Round-trip ms include the injected proxy latency and
  are NOT database work;
- postgres CPU seconds per second (cgroup usage_usec deltas, pg_cpu.jsonl): the actual database work;
- goal-client HTTP: attempts, 409 conflicts, gate rejections (503 + Retry-After), client timeouts, 422s;
- back-pressure: goal requests sent before an earlier 503's Retry-After had elapsed (same goal);
- model pacing: call latency p50 by window, `length` stops, context-window exhaustion, provider failures.
Every rate over goals divides by goals scheduled.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.metrics import TrialData, backend_frame, read_jsonl


def overlap_s(start: np.ndarray, dur_s: np.ndarray, a: float, b: float) -> np.ndarray:
    return np.clip(np.minimum(start + dur_s, b) - np.maximum(start, a), 0, None)


def window_stats(be: pd.DataFrame, a: float, b: float, bg_timeout_ms: float) -> dict:
    start, dur = be.t.to_numpy(), be.total_ms.to_numpy() / 1000
    occ = overlap_s(start, dur, a, b)
    goal = be.goal_id.notna().to_numpy()
    arrived = (start >= a) & (start < b)
    bg = (be.actor == "background").to_numpy()
    ok_bg = bg & arrived & (be.status.between(200, 299).to_numpy()) & (be.total_ms.to_numpy() <= bg_timeout_ms)
    rt = be.db_ms.to_numpy()
    tot_occ, tot_rt = occ.sum(), rt[arrived].sum()
    return {"bg_offered": int((bg & arrived).sum()), "bg_succeeded": int(ok_bg.sum()),
            "goal_inflight_share": float(occ[goal].sum() / tot_occ) if tot_occ else math.nan,
            "goal_db_roundtrip_share": float(rt[arrived & goal].sum() / tot_rt) if tot_rt else math.nan,
            "goal_requests": int((arrived & goal).sum()),
            "gate_rejected_bg": int((arrived & bg & (be.gate == "rejected").to_numpy()).sum()),
            "gate_rejected_goal": int((arrived & goal & (be.gate == "rejected").to_numpy()).sum())}


def pg_cpu_rate(rows: list[dict], t0_ns: int, a: float, b: float) -> float:
    pts = sorted(((r["ts_mono_ns"] - t0_ns) / 1e9, r["usage_usec"]) for r in rows if r.get("usage_usec") is not None)
    inside = [p for p in pts if a <= p[0] <= b]
    if len(inside) < 2:
        return math.nan
    (ta, ua), (tb, ub) = inside[0], inside[-1]
    return (ub - ua) / 1e6 / (tb - ta) if tb > ta else math.nan


def backpressure(client: list[dict], t0_ns: int) -> dict:
    by_goal: dict[str, list[dict]] = {}
    for e in client:
        if e.get("event_type") == "http.attempt" and e.get("goal_id"):
            by_goal.setdefault(e["goal_id"], []).append(e)
    refusals = followed = early = 0
    for evs in by_goal.values():
        evs.sort(key=lambda e: e["ts_mono_ns"])
        for i, e in enumerate(evs):
            if e.get("http_status") in (429, 503) and e.get("retry_after_s"):
                refusals += 1
                end = e["ts_mono_ns"] + (e.get("latency_ms") or 0) * 1e6
                nxt = next((x for x in evs[i + 1:]), None)
                if nxt is not None:
                    followed += 1
                    early += (nxt["ts_mono_ns"] - end) / 1e9 < e["retry_after_s"] - 0.05
    return {"refusals_with_retry_after": refusals, "followed_by_request": followed, "sent_before_retry_after": early}


def trial_row(d: Path) -> dict | None:
    meta = json.loads((d / "trial.json").read_text())
    row = {"trial": meta["trial_id"], "condition": meta["condition"], "gate": meta["gate"], "block": meta.get("block"),
           "label": meta.get("label"), "final_state": meta.get("final_state"),
           "invalid": "; ".join(meta.get("invalid_reasons") or []),
           "inconclusive": "; ".join(meta.get("inconclusive_reasons") or [])}
    vp = d / "verdict.json"
    if not vp.exists():
        return row
    v = json.loads(vp.read_text())
    td = TrialData(d)
    man = td.manifest
    row.update(model=man["model"]["id"] if meta["condition"] == "agent" else None,
               fault=bool(v["fault"].get("enabled")), status=v["status"],
               invalid="; ".join(v.get("invalid_reasons") or []) or row["invalid"],
               inconclusive="; ".join(v.get("inconclusive_reasons") or []) or row["inconclusive"],
               t_rec_s=v["recovery"]["t_rec_s"], recovery=v["recovery"]["verdict"],
               unhealthy_s=v["recovery"]["unhealthy_seconds_in_window"],
               B_p95_ms=v["recovery"]["refs"]["b_p95"], lost_service_D=v["background"]["lost_service_D"],
               goals=v["goals"]["scheduled"], success_S=v["goals"]["success_S"], F_60s=v["goals"]["F_60s"],
               canary_rise_ms=v["fault"].get("canary_rise_ms"))
    for k in ("correct", "incorrect", "duplicate_effect", "wrongly_deferred", "failed", "unfinished", "rejected",
              "provider_failed"):
        row[f"n_{k}"] = v["goals"]["verdicts"].get(k, 0)
    t_on, t_off = v["fault"]["t_on"], v["fault"]["t_off"]
    be = backend_frame(td)
    bg_to = man["background"]["timeout_s"] * 1000
    for name, (a, b) in {"fault": (t_on, t_off), "post60": (t_off, t_off + 60)}.items():
        for k, x in window_stats(be, a, b, bg_to).items():
            row[f"{name}_{k}"] = x
    pg = read_jsonl(d / "pg_cpu.jsonl")
    base = man["windows"]["baseline"]
    row["pg_cpu_s_per_s_baseline"] = pg_cpu_rate(pg, td.t0, base[0], base[1])
    row["pg_cpu_s_per_s_fault"] = pg_cpu_rate(pg, td.t0, t_on, t_off)
    row["pg_cpu_s_per_s_post60"] = pg_cpu_rate(pg, td.t0, t_off, t_off + 60)
    goal_att = [e for e in td.client if e.get("event_type") == "http.attempt" and e.get("goal_id")]
    st = pd.Series([e.get("status") for e in goal_att], dtype=object)
    hs = pd.Series([e.get("http_status") for e in goal_att], dtype=object)
    row.update(goal_http_attempts=len(goal_att), goal_409=int((hs == 409).sum()), goal_503=int((hs == 503).sum()),
               goal_422=int((hs == 422).sum()), goal_timeouts=int((st == "timeout").sum()),
               goal_attempts_per_scheduled=len(goal_att) / v["goals"]["scheduled"] if v["goals"]["scheduled"] else None)
    row.update(backpressure(td.client, td.t0))
    mc = [m for m in td.model_calls if m.get("event_type") == "model.call"]
    if mc:
        lat = pd.Series([(m.get("latency_ms"), td.t(m["ts_mono_ns"])) for m in mc if m.get("latency_ms")])
        lat_df = pd.DataFrame(list(lat), columns=["ms", "t"]) if len(lat) else pd.DataFrame(columns=["ms", "t"])
        row.update(model_calls=len(mc), model_ms_p50=lat_df.ms.median(),
                   model_ms_p50_baseline=lat_df[(lat_df.t >= base[0]) & (lat_df.t < base[1])].ms.median(),
                   model_ms_p50_observe=lat_df[(lat_df.t >= t_off)].ms.median(),
                   model_length_stops=sum(m.get("done_reason") == "length" for m in mc),
                   model_ctx_exhausted=sum(m.get("done_reason") == "length" and m.get("num_ctx")
                                           and (m.get("context_used") or 0) >= m["num_ctx"] - 8 for m in mc),
                   model_provider_failed=sum(m.get("status") == "provider_failed" for m in mc))
    row.update(runner_lag_p99_ms=v["runner"]["loop_lag_p99_ms"], runner_cpu_p95=v["runner"]["cpu_p95_pct"],
               throttled=json.dumps({k: (x or {}).get("nr_throttled") for k, x in
                                     (meta.get("throttling_delta") or {}).items()}),
               gate_baseline_rejection_share=(v.get("gate_stats") or {}).get("baseline_rejection_share"))
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment_dir")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    rows = [r for d in sorted(Path(a.experiment_dir).glob("tr_*")) if (r := trial_row(d))]
    df = pd.DataFrame(rows)
    out = a.out or str(Path(a.experiment_dir) / "campaign.csv")
    df.to_csv(out, index=False)
    with pd.option_context("display.max_columns", None, "display.width", 250):
        print(df.T.to_string())


if __name__ == "__main__":
    main()
