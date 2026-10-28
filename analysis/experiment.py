"""Experiment-level analysis over trial bundles: cell summaries, amplification vs replay, gate acceptance criteria.

Trials are grouped into cells by (condition, gate, fault duration). Only trials of one configuration are pooled:
gate on and gate off differ only in the per-trial gate switch, which is not part of the configuration hash.
Invalid, inconclusive and cancelled trials are listed with their reasons and excluded from cell statistics.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

import pandas as pd


def load_verdicts(exp_dir: str | Path) -> list[dict]:
    out = []
    for d in sorted(Path(exp_dir).glob("tr_*")):
        v = d / "verdict.json"
        meta = json.loads((d / "trial.json").read_text()) if (d / "trial.json").exists() else {}
        if v.exists():
            r = json.loads(v.read_text())
        else:
            r = {"trial_id": d.name, "status": "no_verdict", "condition": meta.get("condition"),
                 "gate": meta.get("gate"), "invalid_reasons": meta.get("invalid_reasons", []),
                 "inconclusive_reasons": meta.get("inconclusive_reasons", [])}
        r["dir"] = str(d)
        r["final_state"] = meta.get("final_state", r.get("final_state"))
        r["block"] = meta.get("block", r.get("block"))
        r["label"] = meta.get("label", r.get("label"))
        out.append(r)
    return out


def row(r: dict) -> dict:
    rec = r.get("recovery", {})
    goals = r.get("goals", {})
    cohorts = {c["cohort"]: c for c in r.get("cohorts", [])}
    vd = goals.get("verdicts", {}) or {}
    return {
        "trial_id": r["trial_id"], "condition": r.get("condition"), "gate": r.get("gate"), "block": r.get("block"),
        "label": r.get("label"), "status": r.get("status"), "fault_s": r.get("fault", {}).get("duration_s"),
        "fault_on": r.get("fault", {}).get("enabled"),
        "t_rec_s": rec.get("t_rec_s"), "recovery": rec.get("verdict"),
        "unhealthy_s": rec.get("unhealthy_seconds_in_window"),
        "scheduled": goals.get("scheduled"), "correct": vd.get("correct", 0), "S": goals.get("success_S"),
        "incorrect": vd.get("incorrect", 0), "duplicate": vd.get("duplicate_effect", 0),
        "failed": vd.get("failed", 0), "unfinished": vd.get("unfinished", 0), "rejected": vd.get("rejected", 0),
        "wrongly_deferred": vd.get("wrongly_deferred", 0), "provider_failed": vd.get("provider_failed", 0),
        "F60": goals.get("F_60s"), "F180": goals.get("F_180s"),
        "D": r.get("background", {}).get("lost_service_D"),
        "bg_p95_baseline": r.get("background", {}).get("baseline_p95_ms"),
        "bg_success_baseline": r.get("background", {}).get("baseline_success"),
        "baseline_S": cohorts.get("baseline", {}).get("success"),
        "R_fault": cohorts.get("fault", {}).get("R_attempts_per_goal"),
        "R_post": cohorts.get("post", {}).get("R_attempts_per_goal"),
        "R_baseline": cohorts.get("baseline", {}).get("R_attempts_per_goal"),
        "W_fault": cohorts.get("fault", {}).get("W_db_ms_per_goal"),
        "reactions_fault": sum(cohorts.get("fault", {}).get(f"n_{k}", 0) for k in
                               ("same_call_retry", "switch_to_broad_search", "restart", "other")),
        "baseline_rejection_share": r.get("gate_stats", {}).get("baseline_rejection_share"),
        "cost_usd": r.get("model", {}).get("cost_usd"), "goal_db_share": r.get("db_time", {}).get(
            "baseline_goal_share"),
        "useful_ratio": r.get("useful_service", {}).get("ratio"),
        "invalid_reasons": "; ".join(r.get("invalid_reasons", []) + r.get("inconclusive_reasons", [])),
    }


def summarize(exp_dir: str | Path) -> dict:
    trials = load_verdicts(exp_dir)
    df = pd.DataFrame([row(r) for r in trials])
    valid = df[df.status == "valid"] if len(df) else df
    cells = []
    if len(valid):
        for (cond, gate, fault_s, fault_on), g in valid.groupby(["condition", "gate", "fault_s", "fault_on"],
                                                               dropna=False):
            cells.append({
                "condition": cond, "gate": gate, "fault_s": fault_s, "fault_on": fault_on, "n": len(g),
                "t_rec_s": list(g.t_rec_s), "recovery": list(g.recovery),
                "S_mean": g.S.mean(), "correct_mean": g.correct.mean(), "D_mean": g.D.mean(),
                "R_fault_mean": g.R_fault.mean(), "incorrect": int(g.incorrect.sum()),
                "duplicate": int(g.duplicate.sum()), "cost_usd": g.cost_usd.fillna(0).sum(),
            })
    out = {"trials": df.to_dict(orient="records"), "cells": cells}
    out["amplification_vs_replay"] = amplification(valid)
    out["acceptance"] = acceptance(valid)
    return out


def amplification(valid: pd.DataFrame) -> dict:
    """R_C(condition) / R_C(replay), gate off, standard fault, by cohort (means over valid trials)."""
    if not len(valid):
        return {}
    std = valid[(valid.gate == "off") & (valid.fault_s == 10) & (valid.fault_on == True)]  # noqa: E712
    rep = std[std.condition == "replay"]
    out = {}
    for cond in ("agent", "scripted"):
        c = std[std.condition == cond]
        if len(c) and len(rep):
            out[cond] = {k: _ratio(c[k].mean(), rep[k].mean()) for k in ("R_baseline", "R_fault", "R_post")}
    return out


def _ratio(a, b):
    return a / b if b and not (isinstance(b, float) and math.isnan(b)) else None


def acceptance(valid: pd.DataFrame) -> dict:
    """The five frozen criteria, judged on agent cells, paired by block."""
    if not len(valid):
        return {}
    res = {}
    for fault_s in sorted(valid.fault_s.dropna().unique()):
        a = valid[(valid.condition == "agent") & (valid.fault_s == fault_s) & (valid.fault_on == True)]  # noqa: E712
        off, on = a[a.gate == "off"], a[a.gate == "on"]
        if not len(off) or not len(on):
            continue
        pairs = []
        for b in sorted(set(off.block.dropna()) & set(on.block.dropna())):
            o, n = off[off.block == b].iloc[0], on[on.block == b].iloc[0]
            pairs.append({"block": b, "t_rec_off": o.t_rec_s, "t_rec_on": n.t_rec_s,
                          "faster_or_both_fast": _faster_or_both_fast(o, n),
                          "both_fast": o.recovery == "fast" and n.recovery == "fast",
                          "correct_off": o.correct, "correct_on": n.correct, "D_off": o.D, "D_on": n.D})
        if not pairs:  # unpaired (e.g. unfamiliar-fault trials without blocks): compare means
            pairs = [{"block": None, "t_rec_off": _med(off.t_rec_s), "t_rec_on": _med(on.t_rec_s),
                      "faster_or_both_fast": None, "both_fast": bool((off.recovery == "fast").all() and
                                                                     (on.recovery == "fast").all()),
                      "correct_off": off.correct.mean(), "correct_on": on.correct.mean(),
                      "D_off": off.D.mean(), "D_on": on.D.mean()}]
        c1 = {
            "baseline_S_change": _rel(on.baseline_S.mean(), off.baseline_S.mean()),
            "baseline_bg_p95_change": _rel(on.bg_p95_baseline.mean(), off.bg_p95_baseline.mean()),
            "baseline_rejection_share_max": on.baseline_rejection_share.max(),
        }
        c1["pass"] = bool(abs(c1["baseline_S_change"] or 0) <= 0.05 and abs(c1["baseline_bg_p95_change"] or 0) <= 0.05
                          and (c1["baseline_rejection_share_max"] or 0) <= 0.01)
        faster = [p["faster_or_both_fast"] for p in pairs if p["faster_or_both_fast"] is not None]
        c2 = {"pairs_faster_or_both_fast": sum(faster), "pairs": len(faster),
              "pass": sum(faster) >= 2 if len(faster) >= 3 else None,
              "gate_not_needed": all(p["both_fast"] for p in pairs)}
        c3 = {"correct_on_minus_off": [p["correct_on"] - p["correct_off"] for p in pairs],
              "incorrect_off": int(off.incorrect.sum()), "incorrect_on": int(on.incorrect.sum()),
              "duplicate_off": int(off.duplicate.sum()), "duplicate_on": int(on.duplicate.sum())}
        c3["pass"] = bool(all(d >= -3 for d in c3["correct_on_minus_off"]) and
                          c3["incorrect_on"] <= c3["incorrect_off"] and c3["duplicate_on"] <= c3["duplicate_off"])
        c4 = {"D_off": [p["D_off"] for p in pairs], "D_on": [p["D_on"] for p in pairs]}
        c4["pass"] = bool(sum(c4["D_on"]) <= sum(c4["D_off"]))
        res[f"fault_{int(fault_s)}s"] = {"pairs": pairs, "c1_healthy_overhead": c1, "c2_recovery": c2,
                                         "c3_useful_work": c3, "c4_ordinary_users": c4}
    return res


def _faster_or_both_fast(o, n) -> bool:
    if o.recovery == "fast" and n.recovery == "fast":
        return True
    t_off = o.t_rec_s if o.t_rec_s is not None and not _isnan(o.t_rec_s) else math.inf
    t_on = n.t_rec_s if n.t_rec_s is not None and not _isnan(n.t_rec_s) else math.inf
    return t_on < t_off


def _isnan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)


def _med(s):
    vals = [x for x in s if x is not None and not _isnan(x)]
    return statistics.median(vals) if vals else None


def _rel(a, b):
    if a is None or b is None or _isnan(a) or _isnan(b) or b == 0:
        return None
    return a / b - 1
