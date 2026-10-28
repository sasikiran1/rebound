"""Experiment-level analysis over trial bundles: cell summaries, amplification vs replay, gate acceptance criteria.

Trials are grouped into cells by (compatibility fingerprint, condition, gate, fault duration). Gate on and off differ
only in the per-trial gate switch, which is not part of the configuration hash; fault duration and fault on/off are the
only declared treatment fields. Evidence from more than one fingerprint or analyzer is partitioned and never judged.
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
        "config_hash": r.get("config_hash"),
        # Bundles written before 2026-10-28 have no compatibility fingerprint: their full config hash stands in.
        "compat": r.get("compat_fingerprint") or r.get("config_hash"),
        "analysis": r.get("analysis_fingerprint"),
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
    """Cells, amplification and acceptance over valid trials of ONE compatibility fingerprint and ONE analyzer.
    Mixed evidence is reported, partitioned in the cell table, and never judged (audit finding 2)."""
    trials = load_verdicts(exp_dir)
    df = pd.DataFrame([row(r) for r in trials])
    valid = df[df.status == "valid"] if len(df) else df
    fingerprints = sorted(valid.compat.dropna().unique()) if len(valid) else []
    analyzers = sorted(valid.analysis.dropna().unique()) if len(valid) else []
    problems = []
    if len(fingerprints) > 1:
        problems.append(f"valid trials come from {len(fingerprints)} configurations: {fingerprints}")
    if len(analyzers) > 1 or (len(analyzers) == 1 and valid.analysis.isna().any()):
        problems.append("verdicts were produced by different analyzers; re-analyze every bundle with one analyzer")
    cells = []
    if len(valid):
        for (fp, cond, gate, fault_s, fault_on), g in valid.groupby(
                ["compat", "condition", "gate", "fault_s", "fault_on"], dropna=False):
            cells.append({
                "compat_fingerprint": fp, "condition": cond, "gate": gate, "fault_s": fault_s, "fault_on": fault_on,
                "n": len(g), "t_rec_s": list(g.t_rec_s), "recovery": list(g.recovery),
                "S_mean": g.S.mean(), "correct_mean": g.correct.mean(), "D_mean": g.D.mean(),
                "R_fault_mean": g.R_fault.mean(), "incorrect": int(g.incorrect.sum()),
                "duplicate": int(g.duplicate.sum()), "cost_usd": g.cost_usd.fillna(0).sum(),
            })
    out = {"trials": df.to_dict(orient="records"), "cells": cells, "mixed_configurations": len(fingerprints) > 1,
           "fingerprints": fingerprints, "problems": problems}
    if problems:
        out["amplification_vs_replay"] = {}
        out["acceptance"] = _not_judged(problems)
    else:
        out["amplification_vs_replay"] = amplification(valid)
        out["acceptance"] = acceptance(valid)
    return out


def amplification(valid: pd.DataFrame) -> dict:
    """R_C(condition) / R_C(replay), gate off, standard fault, by cohort (means over valid trials)."""
    if not len(valid):
        return {}
    std = valid[(valid.gate == "off") & (valid.fault_s == STANDARD_FAULT_S) & (valid.fault_on == True)]  # noqa: E712
    rep = std[std.condition == "replay"]
    out = {}
    for cond in ("agent", "scripted"):
        c = std[std.condition == cond]
        if len(c) and len(rep):
            out[cond] = {k: _ratio(c[k].mean(), rep[k].mean()) for k in ("R_baseline", "R_fault", "R_post")}
    return out


def _ratio(a, b):
    return a / b if b and not (isinstance(b, float) and math.isnan(b)) else None


STANDARD_FAULT_S = 10
MIN_PAIRS = 3  # docs/protocol.md section 7: three paired blocks
CRITERIA = ("c1_healthy_overhead", "c2_recovery", "c3_useful_work", "c4_ordinary_users", "c5_unfamiliar_fault")


def _finite(x) -> bool:
    return x is not None and not _isnan(x) and not (isinstance(x, float) and math.isinf(x))


def _judged(verdict: str, **fields) -> dict:
    return {"verdict": verdict, "pass": {"pass": True, "fail": False}.get(verdict), **fields}


def _insufficient(reason: str, **fields) -> dict:
    return _judged("insufficient_evidence", reason=reason, **fields)


def _not_judged(problems: list[str]) -> dict:
    return {"criteria": {c: _insufficient("; ".join(problems)) for c in CRITERIA},
            "overall": "insufficient_evidence", "problems": problems}


def overall(criteria: dict) -> str:
    verdicts = [c["verdict"] for c in criteria.values()]
    if "fail" in verdicts:
        return "fail"
    return "pass" if all(v == "pass" for v in verdicts) else "insufficient_evidence"


def acceptance(valid: pd.DataFrame) -> dict:
    """The five frozen criteria (docs/protocol.md section 7), judged on agent cells, paired by block. Every criterion
    ends in pass, fail or insufficient_evidence; missing or non-finite inputs are never treated as a pass."""
    res: dict = {"standard_fault_s": STANDARD_FAULT_S, "min_pairs": MIN_PAIRS, "problems": []}
    if not len(valid):
        return {**res, **_not_judged(["no valid trials"])}
    agents = valid[(valid.condition == "agent") & (valid.fault_on == True)]  # noqa: E712
    per = {f"fault_{int(f)}s": fault_cell(agents[agents.fault_s == f])
           for f in sorted(agents.fault_s.dropna().unique())}
    res.update(per)
    std = per.get(f"fault_{STANDARD_FAULT_S}s")
    crit = {}
    for c in CRITERIA[:4]:
        crit[c] = std[c] if std else _insufficient("no standard-fault agent trials with both gate settings")
    unfamiliar = {k: v for k, v in per.items() if k != f"fault_{STANDARD_FAULT_S}s"}
    crit["c5_unfamiliar_fault"] = c5(unfamiliar)
    for cell in per.values():
        res["problems"] += cell["problems"]
    res["criteria"] = crit
    res["overall"] = overall(crit)
    return res


def fault_cell(a: pd.DataFrame) -> dict:
    off, on = a[a.gate == "off"], a[a.gate == "on"]
    problems = []
    dup = set()
    for name, arm in (("off", off), ("on", on)):
        counts = arm.block.dropna().value_counts()
        for b in counts[counts > 1].index:
            dup.add(b)
            problems.append(f"duplicate gate-{name} trials in block {b} ({counts[b]}): block excluded from pairing")
    pairs = []
    for b in sorted((set(off.block.dropna()) & set(on.block.dropna())) - dup):
        o, n = off[off.block == b].iloc[0], on[on.block == b].iloc[0]
        pairs.append({"block": b, "trial_off": o.trial_id, "trial_on": n.trial_id, "t_rec_off": o.t_rec_s,
                      "t_rec_on": n.t_rec_s, "faster_or_both_fast": _faster_or_both_fast(o, n),
                      "both_fast": o.recovery == "fast" and n.recovery == "fast",
                      "correct_off": o.correct, "correct_on": n.correct, "incorrect_off": o.incorrect,
                      "incorrect_on": n.incorrect, "duplicate_off": o.duplicate, "duplicate_on": n.duplicate,
                      "D_off": o.D, "D_on": n.D})
    cell = {"n_off": len(off), "n_on": len(on), "pairs": pairs, "problems": problems,
            "unpaired_means": _means(off, on) if len(off) and len(on) else None}

    # c1: healthy-period overhead, arm means over all valid trials of the cell.
    s_off, s_on = off.baseline_S.mean(), on.baseline_S.mean()
    p_off, p_on = off.bg_p95_baseline.mean(), on.bg_p95_baseline.mean()
    rej = on.baseline_rejection_share.max()
    if not (len(off) and len(on)):
        cell["c1_healthy_overhead"] = _insufficient("needs gate-off and gate-on trials")
    elif not all(_finite(x) for x in (s_off, s_on, p_off, p_on, rej)) or s_off == 0 or p_off == 0:
        cell["c1_healthy_overhead"] = _insufficient("baseline success, baseline p95 or rejection share missing")
    else:
        ds, dp = s_on / s_off - 1, p_on / p_off - 1
        ok = abs(ds) <= 0.05 and abs(dp) <= 0.05 and rej <= 0.01
        cell["c1_healthy_overhead"] = _judged("pass" if ok else "fail", baseline_S_change=ds,
                                              baseline_bg_p95_change=dp, baseline_rejection_share_max=rej)

    if len(pairs) < MIN_PAIRS:
        why = f"{len(pairs)} complete paired blocks, {MIN_PAIRS} required"
        for c in ("c2_recovery", "c3_useful_work", "c4_ordinary_users"):
            cell[c] = _insufficient(why)
        return cell
    faster = [p["faster_or_both_fast"] for p in pairs]
    cell["c2_recovery"] = _judged("pass" if sum(faster) >= 2 else "fail", pairs_faster_or_both_fast=sum(faster),
                                  pairs=len(faster), gate_not_needed=all(p["both_fast"] for p in pairs))
    vals = [p[k] for p in pairs for k in ("correct_off", "correct_on", "incorrect_off", "incorrect_on",
                                          "duplicate_off", "duplicate_on")]
    if not all(_finite(v) for v in vals):
        cell["c3_useful_work"] = _insufficient("goal verdict counts missing")
    else:
        diffs = [p["correct_on"] - p["correct_off"] for p in pairs]
        inc = (sum(p["incorrect_on"] for p in pairs), sum(p["incorrect_off"] for p in pairs))
        dup_ = (sum(p["duplicate_on"] for p in pairs), sum(p["duplicate_off"] for p in pairs))
        ok = all(d >= -3 for d in diffs) and inc[0] <= inc[1] and dup_[0] <= dup_[1]
        cell["c3_useful_work"] = _judged("pass" if ok else "fail", correct_on_minus_off=diffs,
                                         incorrect_on=inc[0], incorrect_off=inc[1], duplicate_on=dup_[0],
                                         duplicate_off=dup_[1])
    d_off, d_on = [p["D_off"] for p in pairs], [p["D_on"] for p in pairs]
    if not all(_finite(v) for v in d_off + d_on):
        cell["c4_ordinary_users"] = _insufficient("lost background service D missing")
    else:
        cell["c4_ordinary_users"] = _judged("pass" if sum(d_on) <= sum(d_off) else "fail", D_off=d_off, D_on=d_on)
    return cell


def _means(off: pd.DataFrame, on: pd.DataFrame) -> dict:
    return {"t_rec_off": _med(off.t_rec_s), "t_rec_on": _med(on.t_rec_s),
            "all_fast_off": bool((off.recovery == "fast").all()), "all_fast_on": bool((on.recovery == "fast").all()),
            "correct_off": off.correct.mean(), "correct_on": on.correct.mean(),
            "incorrect_off": off.incorrect.mean(), "incorrect_on": on.incorrect.mean(),
            "duplicate_off": off.duplicate.mean(), "duplicate_on": on.duplicate.mean(),
            "D_off": off.D.mean(), "D_on": on.D.mean()}


def c5(unfamiliar: dict) -> dict:
    """Unfamiliar fault: the directions of criteria 2-4 must hold in the available trials (weaker evidence)."""
    if not unfamiliar:
        return _insufficient("no unfamiliar-fault agent trials")
    checks = {}
    for key, cell in unfamiliar.items():
        m = cell.get("unpaired_means")
        if m is None:
            return _insufficient(f"{key}: needs gate-off and gate-on trials")
        if not all(_finite(m[k]) for k in ("correct_off", "correct_on", "D_off", "D_on")):
            return _insufficient(f"{key}: verdict counts or D missing")
        t_off = m["t_rec_off"] if _finite(m["t_rec_off"]) else math.inf
        t_on = m["t_rec_on"] if _finite(m["t_rec_on"]) else math.inf
        checks[key] = {
            "recovery": (m["all_fast_off"] and m["all_fast_on"]) or t_on < t_off,
            "useful_work": m["correct_on"] >= m["correct_off"] - 3 and m["incorrect_on"] <= m["incorrect_off"]
            and m["duplicate_on"] <= m["duplicate_off"],
            "ordinary_users": m["D_on"] <= m["D_off"],
        }
    ok = all(all(c.values()) for c in checks.values())
    return _judged("pass" if ok else "fail", directions=checks, note="weaker evidence: unpaired, few trials")


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
