"""Deterministic per-trial analysis: pandas over the trial's JSONL files only.

Recovery and background service use backend-side data only (backend.jsonl, samples.jsonl). Correctness comes only
from oracle.jsonl. Every rate divides by goals scheduled.

"DB ms" everywhere is the summed wall time of a request's database round trips as seen by FieldDesk (queries, BEGIN,
COMMIT, pool reset). It includes the proxy hop and therefore the injected latency; it is not PostgreSQL CPU or
intrinsic query work (audit finding 10).

metrics_version 2 (2026-10-28) adds, without changing any version 1 value: commit-acknowledgment completion times,
fault-window exposure shares, back-pressure (Retry-After) compliance, and analyzer/compatibility fingerprints.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

from analysis import recovery, retries
from harness.manifest import analysis_fingerprint

METRICS_VERSION = 2
RETRY_AFTER_TOLERANCE_S = 0.05  # timer and logging resolution; a request this early is not counted as a violation

COHORTS = {"baseline": (60, 150), "fault": (150, 250), "post": (250, 550)}
ROUTE_PATTERNS = [
    (re.compile(r"^/customers/\d+/appointments$"), "/customers/{customer_id}/appointments"),
    (re.compile(r"^/appointments/\d+/reschedule$"), "/appointments/{appointment_id}/reschedule"),
    (re.compile(r"^/appointments/\d+$"), "/appointments/{appointment_id}"),
]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def route_template(method: str, path: str) -> str:
    for pat, tmpl in ROUTE_PATTERNS:
        if pat.match(path):
            return f"{method} {tmpl}"
    return f"{method} {path}"


def actor_kind(actor_id: str | None) -> str:
    if not actor_id:
        return "unknown"
    return actor_id.split("_")[0]


def _nan_to_none(x):
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, (np.floating,)):
        return None if np.isnan(x) else float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return x


def clean(obj):
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return _nan_to_none(obj)


class TrialData:
    def __init__(self, trial_dir: str | Path):
        self.dir = Path(trial_dir)
        self.meta = json.loads((self.dir / "trial.json").read_text())
        self.manifest = json.loads((self.dir / "manifest.resolved.json").read_text())
        self.t0 = self.meta["t0_mono_ns"]
        self.t0_wall = dt.datetime.fromisoformat(self.meta["t0_wall"].replace("Z", "+00:00"))
        self.client = read_jsonl(self.dir / "client.jsonl")
        self.background_client = read_jsonl(self.dir / "background.jsonl")
        self.model_calls = read_jsonl(self.dir / "model_calls.jsonl")
        self.backend = read_jsonl(self.dir / "backend.jsonl")
        self.samples = read_jsonl(self.dir / "samples.jsonl")
        self.fault = read_jsonl(self.dir / "fault.jsonl")
        self.oracle = read_jsonl(self.dir / "oracle.jsonl")
        self.runner = read_jsonl(self.dir / "runner.jsonl")

    def t(self, mono_ns: int) -> float:
        return (mono_ns - self.t0) / 1e9

    def t_wall(self, iso: str) -> float:
        return (dt.datetime.fromisoformat(iso.replace("Z", "+00:00")) - self.t0_wall).total_seconds()


def backend_frame(td: TrialData) -> pd.DataFrame:
    rows = []
    for r in td.backend:
        if r.get("trial_id") != td.meta["trial_id"]:
            continue
        rows.append({
            "t": td.t(r["ts_mono_ns"]), "actor": actor_kind(r.get("actor_id")), "goal_id": r.get("goal_id"),
            "attempt_id": r.get("attempt_id"), "route": route_template(r["method"], r["path"]),
            "status": r["http_status"], "total_ms": r.get("total_ms", r.get("handler_ms")), "db_ms": r["db_ms"],
            "queue_wait_ms": r.get("queue_wait_ms", 0.0), "gate": r.get("gate_decision", "admitted"),
            "commit_ack_t": td.t(r["commit_ack_mono_ns"]) if r.get("commit_ack_mono_ns") else math.nan,
        })
    cols = ["t", "actor", "goal_id", "attempt_id", "route", "status", "total_ms", "db_ms", "queue_wait_ms", "gate",
            "commit_ack_t"]
    return pd.DataFrame(rows, columns=cols)


def samples_frame(td: TrialData) -> pd.DataFrame:
    df = pd.DataFrame([{**s, "t": td.t(s["ts_mono_ns"])} for s in td.samples])
    if df.empty:
        return pd.DataFrame(columns=["t", "in_flight", "pool_waiters", "pool_in_use", "gate_queue", "cpu_pct",
                                     "loop_lag_ms", "gate_rejected_total"])
    return df


def goals_frame(td: TrialData) -> pd.DataFrame:
    goals: dict[str, dict] = {}
    for e in td.client:
        g = e.get("goal_id")
        if g is None:
            continue
        rec = goals.setdefault(g, {"goal_id": g})
        if e["event_type"] == "goal.scheduled":
            rec.update(t_sched=td.t(e["ts_mono_ns"]), task_id=e["task_id"], kind=e.get("kind"),
                       expected=e.get("expected"))
        elif e["event_type"] == "goal.started":
            rec["t_start"] = td.t(e["ts_mono_ns"])
        elif e["event_type"] == "goal.ended":
            rec.update(t_end=td.t(e["ts_mono_ns"]), end_reason=e["outcome"], limit=e.get("limit"),
                       model_calls=e.get("model_calls"), cost_usd=e.get("cost_usd"),
                       invalid_arguments=e.get("invalid_arguments"), gate_rejections=e.get("gate_rejections"))
    for v in td.oracle:
        rec = goals.setdefault(v["goal_id"], {"goal_id": v["goal_id"]})
        rec["verdict"] = v["verdict"]
        rec["oracle_end_reason"] = v.get("end_reason")
        # INSERT time of the goal's change (named `completed_at` in bundles written before 2026-10-28; same value).
        ins = v.get("change_inserted_at") or v.get("completed_at")
        rec["t_change_insert"] = td.t_wall(ins) if ins else math.nan
    df = pd.DataFrame(list(goals.values()))
    for c in ("goal_id", "t_sched", "t_change_insert", "verdict", "kind", "expected"):
        if c not in df:
            df[c] = math.nan
    return df.sort_values("t_sched").reset_index(drop=True)


def cohort_of(t: float) -> str | None:
    for name, (a, b) in COHORTS.items():
        if a <= t < b:
            return name
    return None


def analyze(trial_dir: str | Path, out_dir: str | Path | None = None) -> dict:
    """Analyze one bundle. Derived files go to `out_dir` (default: the bundle itself, as during a trial). Re-analysis
    of a saved bundle writes elsewhere so the recorded evidence is never overwritten."""
    td = TrialData(trial_dir)
    m = td.manifest
    meta = td.meta
    out_dir = Path(out_dir) if out_dir is not None else td.dir
    out_dir.mkdir(parents=True, exist_ok=True)
    be = backend_frame(td)
    samples = samples_frame(td)
    goals = goals_frame(td)
    baseline = tuple(m["windows"]["baseline"])
    observe_s = m["windows"]["observe_s"]
    bg_timeout_ms = m["background"]["timeout_s"] * 1000
    fault_start = m["fault"]["start_s"]
    t_on = next((td.t(e["ts_mono_ns"]) for e in td.fault if e["event_type"] == "fault.applied"), None)
    t_off = next((td.t(e["ts_mono_ns"]) for e in td.fault if e["event_type"] == "fault.removed"), None)
    if t_off is None:  # healthy reference trial: no toxic, nominal times
        t_on, t_off = fault_start, fault_start + m["fault"]["duration_s"]
    observe_end = t_off + observe_s

    # Background service, backend side.
    bg = be[be.actor == "background"].copy()
    bg["latency_ms"] = bg.total_ms
    bg["ok"] = (bg.status >= 200) & (bg.status < 300) & (bg.total_ms <= bg_timeout_ms)
    refs = recovery.reference_values(bg, samples, baseline)
    t_end = int(min(m["arrivals"]["stop_s"] + m["windows"]["drain_deadline_s"], max(bg.t.max() if len(bg) else 0, 0)))
    series = recovery.healthy_series(bg, samples, refs, t_end) if len(bg) and len(samples) else pd.DataFrame()
    healthy = dict(zip(series.t, series.healthy, strict=True)) if len(series) else {}
    t_rec = recovery.recovery_time(healthy, t_off, observe_s)
    rec_verdict = recovery.verdict(t_rec, observe_s)
    unhealthy_s = int(sum(1 for t, h in healthy.items() if t_off <= t < observe_end and not h))

    # Lost background service D over 10 s windows from fault start to the end of observation.
    rate = m["background"]["rate_rps"]
    d_total, d_rows = 0.0, []
    w = math.floor(t_on)
    while w + 10 <= observe_end:
        succ = int(bg[(bg.t >= w) & (bg.t < w + 10)].ok.sum())
        lost = max(0.0, rate * 10 * refs.b_s - succ) if not math.isnan(refs.b_s) else math.nan
        d_rows.append({"t": w, "offered": rate * 10, "succ": succ, "lost": lost})
        d_total += lost
        w += 10

    # Goals.
    n_sched = len(goals)
    verdicts = goals.verdict.value_counts().to_dict() if n_sched else {}
    n_correct = int(verdicts.get("correct", 0))
    # Completion time: the backend's commit acknowledgment where the bundle has it (backend logs from 2026-10-28),
    # else the change's INSERT time, which precedes COMMIT by one DB round trip. The basis is reported.
    acks = be[be.commit_ack_t.notna() & be.goal_id.notna()].groupby("goal_id").commit_ack_t.min() if len(be) else (
        pd.Series(dtype=float))
    goals["t_commit_ack"] = goals.goal_id.map(acks)
    has_ack = bool(goals.t_commit_ack.notna().any())
    completion_basis = "commit_ack" if has_ack else "change_insert"
    goals["latency_s"] = (goals.t_commit_ack if has_ack else goals.t_change_insert) - goals.t_sched
    goals["cohort"] = goals.t_sched.map(cohort_of)

    def completion(tau: float) -> float:
        if not n_sched:
            return math.nan
        ok = (goals.verdict == "correct") & (goals.expected == "reschedule") & (goals.latency_s <= tau)
        feasible = int((goals.expected == "reschedule").sum())
        return float(ok.sum() / feasible) if feasible else math.nan

    # Attempts and DB time per goal (client attempts; backend DB time joined by goal ID).
    client_attempts = [e for e in td.client if e["event_type"] == "http.attempt" and e.get("goal_id")]
    att_per_goal = pd.Series([e["goal_id"] for e in client_attempts]).value_counts() if client_attempts else pd.Series(
        dtype=int)
    goal_be = be[be.goal_id.notna()]
    db_per_goal = goal_be.groupby("goal_id").db_ms.sum() if len(goal_be) else pd.Series(dtype=float)
    goals["attempts"] = goals.goal_id.map(att_per_goal).fillna(0).astype(int)
    goals["db_ms"] = goals.goal_id.map(db_per_goal).fillna(0.0)
    transport_retries_agent = sum(1 for e in client_attempts if e.get("actor_kind") == "agent" and e["attempt_n"] > 1)
    scripted_retries = sum(1 for e in client_attempts if e.get("retry_layer") == "scripted")

    # Retry classes per goal from tool.op sequences.
    ops_by_goal: dict[str, list[dict]] = {}
    for e in td.client:
        if e["event_type"] == "tool.op" and e.get("goal_id") and e["status"] != "invalid_arguments":
            ops_by_goal.setdefault(e["goal_id"], []).append(e)
    class_rows = []
    for g, ops in ops_by_goal.items():
        labels = retries.classify_goal(ops)
        for op, lab in zip(ops, labels, strict=True):
            class_rows.append({"goal_id": g, "tool": op["tool"], "status": op["status"], "label": lab,
                               "t": td.t(op["ts_mono_ns"])})
    classes = pd.DataFrame(class_rows, columns=["goal_id", "tool", "status", "label", "t"])
    classes["cohort"] = classes.goal_id.map(dict(zip(goals.goal_id, goals.cohort, strict=True)))

    cohort_rows = []
    for name in COHORTS:
        cg = goals[goals.cohort == name]
        cc = classes[classes.cohort == name]
        cohort_rows.append({
            "cohort": name, "goals": len(cg),
            "success": float((cg.verdict == "correct").mean()) if len(cg) else math.nan,
            "R_attempts_per_goal": float(cg.attempts.mean()) if len(cg) else math.nan,
            "W_db_ms_per_goal": float(cg.db_ms.mean()) if len(cg) else math.nan,
            **{f"n_{k}": int((cc.label == k).sum()) for k in retries.CLASSES},
        })
    cohorts = pd.DataFrame(cohort_rows)

    # Useful-service check: goals scheduled in the 120 s after recovery vs the baseline cohort.
    useful = {"checked": False}
    base_success = cohorts.set_index("cohort").loc["baseline", "success"]
    if t_rec is not None:
        a = t_off + t_rec
        post = goals[(goals.t_sched >= a) & (goals.t_sched < a + 120)]
        post_s = float((post.verdict == "correct").mean()) if len(post) else math.nan
        useful = {"checked": True, "window": [a, a + 120], "goals": len(post), "success": post_s,
                  "baseline_success": base_success,
                  "ratio": post_s / base_success if base_success and not math.isnan(post_s) else math.nan}
        useful["flag_below_80pct"] = bool(useful["ratio"] < 0.8) if not math.isnan(useful["ratio"]) else None

    # Model calls.
    mc = pd.DataFrame([{**c, "t": td.t(c["ts_mono_ns"])} for c in td.model_calls]) if td.model_calls else pd.DataFrame()
    model = {"calls": 0}
    if len(mc):
        okc = mc[mc.status == "ok"]
        base_lat = okc[(okc.t >= baseline[0]) & (okc.t < baseline[1])].latency_ms.median()
        obs_lat = okc[(okc.t >= t_off) & (okc.t < observe_end)].latency_ms.median()
        model = {
            "calls": len(mc), "ok_calls": len(okc), "cost_usd": float(okc.cost_usd.sum()),
            "input_tokens": int(okc.input_tokens.sum()), "cached_tokens": int(okc.cached_tokens.sum()),
            "output_tokens": int(okc.output_tokens.sum()), "reasoning_tokens": int(okc.reasoning_tokens.sum()),
            "latency_ms_p50": float(okc.latency_ms.median()), "latency_ms_baseline_p50": base_lat,
            "latency_ms_observe_p50": obs_lat, "provider_retries": int(mc.provider_retries.fillna(0).sum()),
            "cost_per_goal_usd": float(okc.cost_usd.sum()) / n_sched if n_sched else math.nan,
        }
    # Agent step time (healthy): gap between consecutive tool ops of a goal, baseline cohort.
    step_gaps = []
    for g, ops in ops_by_goal.items():
        if goals.set_index("goal_id").cohort.get(g) == "baseline":
            ts = [td.t(o["ts_mono_ns"]) for o in ops]
            step_gaps += [b - a for a, b in zip(ts, ts[1:], strict=False)]

    # Gate.
    gate_counts = be.gate.value_counts().to_dict() if len(be) else {}
    base_be = be[(be.t >= baseline[0]) & (be.t < baseline[1])]
    baseline_rejections = float((base_be.gate == "rejected").mean()) if len(base_be) else math.nan

    # DB time accounting.
    def db_rate(df, a, b):
        return float(df[(df.t >= a) & (df.t < b)].db_ms.sum() / (b - a)) if b > a else math.nan

    db = {
        "baseline_total_ms_per_s": db_rate(be, *baseline),
        "baseline_goals_ms_per_s": db_rate(be[be.goal_id.notna()], *baseline),
        "baseline_background_ms_per_s": db_rate(bg, *baseline),
        "observe_total_ms_per_s": db_rate(be, t_off, observe_end),
        "observe_goals_ms_per_s": db_rate(be[be.goal_id.notna()], t_off, observe_end),
    }
    db["baseline_goal_share"] = (db["baseline_goals_ms_per_s"] / db["baseline_total_ms_per_s"]
                                 if db["baseline_total_ms_per_s"] else math.nan)

    # Validity.
    invalid, inconclusive = list(meta.get("invalid_reasons", [])), []
    lags = [r["loop_lag_max_ms"] for r in td.runner]
    cpus = [r["cpu_pct"] for r in td.runner]
    runner = {"loop_lag_p99_ms": float(np.percentile(lags, 99)) if lags else math.nan,
              "cpu_p95_pct": float(np.percentile(cpus, 95)) if cpus else math.nan,
              "cpu_mean_pct": float(np.mean(cpus)) if cpus else math.nan}
    if lags and runner["loop_lag_p99_ms"] >= 50:
        invalid.append(f"runner event-loop lag p99 {runner['loop_lag_p99_ms']:.1f} ms >= 50 ms")
    if cpus and runner["cpu_p95_pct"] >= 70:
        invalid.append(f"runner CPU p95 {runner['cpu_p95_pct']:.0f}% >= 70%")
    for name, d in (meta.get("throttling_delta") or {}).items():
        if d and d.get("nr_throttled", 0) > 0:
            invalid.append(f"{name} CPU throttled {d['nr_throttled']} periods")
    if transport_retries_agent:
        invalid.append(f"{transport_retries_agent} transport retries in the agent path (bug)")
    missing = set(goals.goal_id) - {v["goal_id"] for v in td.oracle}
    if missing:
        inconclusive.append(f"{len(missing)} goals without an oracle verdict")
    if n_sched and meta["condition"] == "agent":
        pf = int(verdicts.get("provider_failed", 0))
        if pf / n_sched > 0.05:
            inconclusive.append(f"provider_failed {pf}/{n_sched} > 5%")
        bl, ol = model.get("latency_ms_baseline_p50"), model.get("latency_ms_observe_p50")
        if bl and ol and not math.isnan(bl) and not math.isnan(ol) and ol > 2 * bl:
            inconclusive.append(f"model latency observe p50 {ol:.0f} ms > 2x baseline {bl:.0f} ms")
    # Clock check: each backend record of a goal attempt must fall inside that client attempt.
    clock = clock_check(td, be)
    if clock["n"] and clock["outside_share"] > 0.01:
        invalid.append(f"clock check: {clock['outside_share']:.1%} of backend records outside their client attempt")

    exposure_res = exposure(be, {"baseline": baseline, "fault": (t_on, t_off), "post60": (t_off, t_off + 60)})
    backpressure_res = backpressure(td)

    status = "invalid" if invalid else ("inconclusive" if inconclusive else "valid")
    if meta.get("final_state") == "CANCELLED":
        status = "cancelled"

    result = {
        "trial_id": meta["trial_id"], "experiment_id": meta["experiment_id"], "condition": meta["condition"],
        "gate": meta["gate"], "repeat": meta.get("repeat"), "block": meta.get("block"), "label": meta.get("label"),
        "config_hash": meta["config_hash"], "compat_fingerprint": meta.get("compat_fingerprint", meta["config_hash"]),
        "analysis_fingerprint": analysis_fingerprint(), "metrics_version": METRICS_VERSION,
        "final_state": meta.get("final_state"), "status": status,
        "invalid_reasons": invalid, "inconclusive_reasons": inconclusive,
        "fault": {"t_on": t_on, "t_off": t_off, "enabled": m.get("fault_enabled", True),
                  "latency_ms": m["fault"]["latency_ms"], "duration_s": m["fault"]["duration_s"],
                  "verified": meta.get("fault_verified"), "canary_rise_ms": meta.get("canary_rise_ms")},
        "recovery": {"t_rec_s": t_rec, "verdict": rec_verdict, "refs": recovery.refs_dict(refs),
                     "unhealthy_seconds_in_window": unhealthy_s, "observe_end": observe_end},
        "background": {"rate_rps": rate, "lost_service_D": d_total,
                       "baseline_success": refs.b_s, "baseline_p95_ms": refs.b_p95, "requests": len(bg)},
        "goals": {"scheduled": n_sched, "verdicts": verdicts, "success_S": n_correct / n_sched if n_sched else None,
                  "F_60s": completion(60), "F_180s": completion(180), "completion_basis": completion_basis},
        "cohorts": cohorts.to_dict(orient="records"),
        "useful_service": useful,
        "retries": {"transport_retries_agent": transport_retries_agent, "scripted_retries": scripted_retries,
                    "classes": classes.label.value_counts().to_dict() if len(classes) else {}},
        "model": model,
        "agent_step_s_p50": float(np.median(step_gaps)) if step_gaps else None,
        "gate_stats": {"decisions": gate_counts, "baseline_rejection_share": baseline_rejections},
        "db_time": db,
        "db_time_definition": "summed DB round-trip elapsed ms per request (includes proxy latency; not DB CPU)",
        "exposure": exposure_res,
        "backpressure": backpressure_res,
        "runner": runner,
        "clock_check": clock,
    }
    result = clean(result)

    out_dir.mkdir(parents=True, exist_ok=True)
    series.to_csv(out_dir / "per_second.csv", index=False)
    goals.to_csv(out_dir / "goals.csv", index=False)
    cohorts.to_csv(out_dir / "cohorts.csv", index=False)
    classes.to_csv(out_dir / "retry_classes.csv", index=False)
    pd.DataFrame(d_rows).to_csv(out_dir / "lost_service.csv", index=False)
    (out_dir / "verdict.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    return result


def clock_check(td: TrialData, be: pd.DataFrame) -> dict:
    """Shared-kernel clock sanity: a backend record's start must lie inside its client attempt (send .. receive)."""
    by_attempt = {(r["attempt_id"]): r["t"] for r in be[be.attempt_id.notna()][["attempt_id", "t"]].to_dict("records")}
    n = outside = 0
    for e in td.client:
        if e["event_type"] != "http.attempt" or e.get("http_status") is None or e["attempt_id"] not in by_attempt:
            continue
        recv = td.t(e["ts_mono_ns"])
        sent = recv - (e["latency_ms"] or 0) / 1000
        tb = by_attempt[e["attempt_id"]]
        n += 1
        if not (sent - 0.005 <= tb <= recv + 0.005):
            outside += 1
    return {"n": n, "outside": outside, "outside_share": outside / n if n else 0.0}


def exposure(be: pd.DataFrame, windows: dict[str, tuple[float, float]]) -> dict:
    """Who occupied the API in each window: in-flight seconds and DB round-trip ms that overlap the window, for goal
    clients (any request carrying a goal ID) and background clients. A request's DB ms is spread evenly over its
    duration. This is occupancy, not causation (audit finding 9: goals were under 1% of healthy DB time)."""
    out = {}
    if not len(be):
        return out
    s = be.t.to_numpy(dtype=float)
    dur = be.total_ms.fillna(0).to_numpy(dtype=float) / 1000
    e = s + dur
    db = be.db_ms.fillna(0).to_numpy(dtype=float)
    goal = be.goal_id.notna().to_numpy()
    bgm = (be.actor == "background").to_numpy()
    rejected = (be.gate == "rejected").to_numpy()
    for name, (a, b) in windows.items():
        if a is None or b is None or b <= a:
            continue
        ov = np.clip(np.minimum(e, b) - np.maximum(s, a), 0, None)
        started = (s >= a) & (s < b)
        frac = np.where(dur > 0, ov / np.where(dur > 0, dur, 1.0), started.astype(float))
        dbw = db * frac
        g_in, b_in = float(ov[goal].sum()), float(ov[bgm].sum())
        g_db, b_db = float(dbw[goal].sum()), float(dbw[bgm].sum())
        out[name] = {
            "window_s": [round(a, 3), round(b, 3)],
            "goal_inflight_s": round(g_in, 2), "background_inflight_s": round(b_in, 2),
            "goal_db_ms": round(g_db, 1), "background_db_ms": round(b_db, 1),
            "goal_share_inflight": g_in / (g_in + b_in) if g_in + b_in else None,
            "goal_share_db": g_db / (g_db + b_db) if g_db + b_db else None,
            "goal_requests_started": int((started & goal).sum()),
            "goal_requests_rejected": int((started & goal & rejected).sum()),
            "background_requests_rejected": int((started & bgm & rejected).sum()),
        }
    return out


def backpressure(td: TrialData, tolerance_s: float = RETRY_AFTER_TOLERANCE_S, max_witnesses: int = 25) -> dict:
    """Retry-After compliance per goal (RFC 9110 section 10.2.3: how long the client ought to wait). After a 429 or
    503 that carried Retry-After, the goal's next HTTP attempt is a violation if it was sent earlier than that.
    Each witness joins the refused attempt, the next attempt and the model call that chose it."""
    att: dict[str, list[tuple]] = {}
    for e in td.client:
        if e.get("event_type") != "http.attempt" or not e.get("goal_id"):
            continue
        recv = td.t(e["ts_mono_ns"])
        sent = recv - (e.get("latency_ms") or 0) / 1000
        att.setdefault(e["goal_id"], []).append((sent, recv, e.get("http_status"), e.get("retry_after_s"),
                                                  e.get("tool"), e.get("attempt_id"), e.get("model_call_id"),
                                                  e.get("actor_kind")))
    n_signal = n_followed = n_viol = 0
    viol_goals: set[str] = set()
    witnesses, gaps, max_burst = [], [], 0
    for g, a in att.items():
        a.sort()
        for i, (_s, recv, st, ra, tool, aid, _mc, _k) in enumerate(a):
            if st in (429, 503) and ra is not None:
                n_signal += 1
                if i + 1 < len(a):
                    n_followed += 1
                    nxt = a[i + 1]
                    gap = nxt[0] - recv
                    gaps.append(gap)
                    if gap < ra - tolerance_s:
                        n_viol += 1
                        viol_goals.add(g)
                        if len(witnesses) < max_witnesses:
                            witnesses.append({"goal_id": g, "refused_attempt_id": aid, "refused_tool": tool,
                                              "t_refused": round(recv, 3), "retry_after_s": ra,
                                              "next_attempt_id": nxt[5], "next_tool": nxt[4],
                                              "next_model_call_id": nxt[6], "gap_s": round(gap, 3)})
        sends = [x[0] for x in a]
        j = 0
        for i in range(len(sends)):
            while sends[i] - sends[j] > 10:
                j += 1
            max_burst = max(max_burst, i - j + 1)
    return {
        "signals": n_signal, "followed_by_attempt": n_followed, "violations": n_viol,
        "violation_share": n_viol / n_followed if n_followed else None,
        "goals_with_violation": len(viol_goals), "tolerance_s": tolerance_s,
        "gap_s_p50": float(np.median(gaps)) if gaps else None,
        "max_attempts_one_goal_10s": max_burst, "witnesses": witnesses,
    }
