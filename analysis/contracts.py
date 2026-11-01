"""Recovery contract (audit 2026-10-27, highest-impact item): one auditable claim per trial, checked by code only.

A contract is a set of declared predicates over a trial's verdict. Each predicate ends in PASS, FAIL or
INSUFFICIENT_EVIDENCE and carries witness IDs that join back to the raw events. The model is the system under test;
nothing here asks a model anything.

Per-trial predicates (contract v1):
- integrity     the trial is valid and its raw evidence passed its checksums; otherwise nothing else is judged.
- business      every scheduled goal has an oracle verdict; no incorrect or duplicate effect; correct goals are at
                least `min_correct_share` of scheduled goals (denominator: all scheduled goals).
- backpressure  after a 429/503 carrying Retry-After, the goal's next request is not sent earlier than Retry-After
                (RFC 9110 section 10.2.3), within a 50 ms measurement tolerance. Insufficient if the trial produced no
                such signal: an unexercised rule is not a pass.
- amplification fault-cohort HTTP attempts per goal at most `max_fault_amplification` times the baseline cohort's
                (2.0 = a client that retries each refused or failed request at most once).

Paired comparison (`compare`): a treatment (e.g. live agents, or agents without the runtime guard) against a control
(e.g. the robust scripted client, or agents with the guard), block by block. An effect is material only if it exceeds
the declared margin in at least 2 complete blocks AND exceeds the spread between control trials (negative control).
Margins were declared in the audit before any confirmatory trial: +5 s recovery, +500 lost background successes,
3 fewer correct goals per 228 scheduled.
"""

from __future__ import annotations

import math

CONTRACT_VERSION = 1
THRESHOLDS = {"min_correct_share": 0.95, "max_fault_amplification": 2.0}
MARGINS = {"t_rec_s": 5.0, "D": 500.0, "correct": 3.0}  # treatment worse by at least this much
PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT_EVIDENCE"


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pred(verdict: str, reason: str, **evidence) -> dict:
    return {"verdict": verdict, "reason": reason, **evidence}


def trial_contract(v: dict, integrity_ok: bool, thresholds: dict | None = None) -> dict:
    """Evaluate contract v1 on one trial's verdict (analysis/metrics.py output)."""
    th = {**THRESHOLDS, **(thresholds or {})}
    preds: dict[str, dict] = {}
    if not integrity_ok:
        preds["integrity"] = _pred(INSUFFICIENT, "raw evidence failed its checksums")
    elif v.get("status") != "valid":
        reasons = v.get("invalid_reasons", []) + v.get("inconclusive_reasons", [])
        preds["integrity"] = _pred(INSUFFICIENT, f"trial status {v.get('status')}: {reasons}")
    else:
        preds["integrity"] = _pred(PASS, "valid trial, raw checksums verified")
    judged = preds["integrity"]["verdict"] == PASS

    goals = v.get("goals", {})
    n, verdicts = goals.get("scheduled") or 0, goals.get("verdicts") or {}
    if not judged or not n:
        preds["business"] = _pred(INSUFFICIENT, "no valid goal outcomes" if judged else "integrity not established")
    else:
        bad = {k: c for k, c in verdicts.items() if k != "correct" and c}
        unverdicted = n - sum(verdicts.values())
        share = verdicts.get("correct", 0) / n
        ok = (unverdicted == 0 and not verdicts.get("incorrect") and not verdicts.get("duplicate_effect")
              and share >= th["min_correct_share"])
        preds["business"] = _pred(PASS if ok else FAIL, f"{verdicts.get('correct', 0)}/{n} correct",
                                  correct_share=share, non_correct=bad, unverdicted=unverdicted)

    bp = v.get("backpressure") or {}
    if not judged:
        preds["backpressure"] = _pred(INSUFFICIENT, "integrity not established")
    elif not bp.get("followed_by_attempt"):
        preds["backpressure"] = _pred(INSUFFICIENT, "no Retry-After signal was followed by a request (not exercised)",
                                      signals=bp.get("signals", 0))
    else:
        preds["backpressure"] = _pred(
            PASS if bp["violations"] == 0 else FAIL,
            f"{bp['violations']} of {bp['followed_by_attempt']} Retry-After signals answered too early",
            violations=bp["violations"], followed=bp["followed_by_attempt"],
            goals_with_violation=bp.get("goals_with_violation"), witnesses=bp.get("witnesses", [])[:10])

    cohorts = {c["cohort"]: c for c in v.get("cohorts", [])}
    r_base, r_fault = (cohorts.get("baseline", {}).get("R_attempts_per_goal"),
                       cohorts.get("fault", {}).get("R_attempts_per_goal"))
    if not judged or not (_finite(r_base) and _finite(r_fault)) or r_base == 0:
        preds["amplification"] = _pred(INSUFFICIENT, "cohort attempt rates missing" if judged else
                                       "integrity not established")
    else:
        ratio = r_fault / r_base
        preds["amplification"] = _pred(PASS if ratio <= th["max_fault_amplification"] else FAIL,
                                       f"fault/baseline attempts per goal = {ratio:.2f}", ratio=ratio,
                                       r_baseline=r_base, r_fault=r_fault)

    verdicts_ = [p["verdict"] for p in preds.values()]
    overall = FAIL if FAIL in verdicts_ else (PASS if all(x == PASS for x in verdicts_) else INSUFFICIENT)
    return {"contract_version": CONTRACT_VERSION, "trial_id": v.get("trial_id"), "condition": v.get("condition"),
            "gate": v.get("gate"), "config_hash": v.get("config_hash"),
            "compat_fingerprint": v.get("compat_fingerprint"), "thresholds": th, "predicates": preds,
            "overall": overall}


def _metric(v: dict, key: str):
    if key == "t_rec_s":
        t = v.get("recovery", {}).get("t_rec_s")
        return t if _finite(t) else math.inf  # not recovered within the window counts as infinitely slow
    if key == "D":
        return v.get("background", {}).get("lost_service_D")
    if key == "correct":
        return (v.get("goals", {}).get("verdicts") or {}).get("correct", 0)
    raise KeyError(key)


def compare(treatment: list[dict], control: list[dict], min_blocks: int = 2, margins: dict | None = None) -> dict:
    """Paired, block-by-block comparison of valid trials. `treatment` worse than `control` by at least the margin is
    an adverse effect. The spread between control trials is the negative-control noise floor."""
    mg = {**MARGINS, **(margins or {})}
    problems = []
    fps = {v.get("compat_fingerprint") or v.get("config_hash") for v in treatment + control}
    if len(fps) > 1:
        problems.append(f"trials come from {len(fps)} configurations; comparison not judged")
    invalid = [v.get("trial_id") for v in treatment + control if v.get("status") != "valid"]
    if invalid:
        problems.append(f"invalid or inconclusive trials excluded: {invalid}")
    t_by = {v.get("block"): v for v in treatment if v.get("status") == "valid"}
    c_by = {v.get("block"): v for v in control if v.get("status") == "valid"}
    blocks = sorted(b for b in set(t_by) & set(c_by) if b is not None)
    out = {"blocks": blocks, "margins": mg, "problems": problems, "metrics": {}}
    for key, worse_sign in (("t_rec_s", 1), ("D", 1), ("correct", -1)):
        diffs = []
        for b in blocks:
            a, c = _metric(t_by[b], key), _metric(c_by[b], key)
            diffs.append(worse_sign * (a - c) if _finite(a) and _finite(c) else (math.inf if a == math.inf != c
                                                                                    else math.nan))
        ctrl = [_metric(v, key) for v in c_by.values()]
        ctrl = [x for x in ctrl if _finite(x)]
        spread = (max(ctrl) - min(ctrl)) if len(ctrl) >= 2 else None
        material = [d for d in diffs if not math.isnan(d) and d >= mg[key] and (spread is None or d > spread)]
        if problems and len(fps) > 1:
            verdict = INSUFFICIENT
        elif len(blocks) < min_blocks or any(math.isnan(d) for d in diffs):
            verdict = INSUFFICIENT
        elif spread is None:
            verdict = INSUFFICIENT  # without repeated controls the noise floor is unknown
        else:
            verdict = "MATERIAL_ADVERSE_EFFECT" if len(material) >= min_blocks else "NO_MATERIAL_ADVERSE_EFFECT"
        out["metrics"][key] = {"treatment_minus_control_worse": diffs, "control_spread": spread,
                               "blocks_material": len(material), "verdict": verdict}
    return out
