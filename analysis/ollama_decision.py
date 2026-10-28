"""Apply the predeclared decision rules of docs/ollama_protocol.md to the campaign table.

Usage: python -m analysis.ollama_decision <campaign.csv>
Rows are matched by block and label (gemma4-A..D, bg-B, bg-D). Only valid trials enter a comparison; a block whose
pair is incomplete is reported and not judged.
"""

from __future__ import annotations

import json
import math
import sys

import pandas as pd


def get(df, block, label):
    """The one VALID trial for this arm (an invalid original plus its declared rerun leaves one valid row)."""
    r = df[(df.block == block) & (df.label == label) & (df.status == "valid")]
    return r.iloc[0] if len(r) == 1 else None


def t_rec(r):
    return math.inf if pd.isna(r.t_rec_s) else float(r.t_rec_s)


def main() -> None:
    df = pd.read_csv(sys.argv[1])
    blocks = sorted(int(b) for b in df.block.dropna().unique() if int(b) > 0)
    per_block, n_unneeded, n_helps, n_hurts, n_ai = [], 0, 0, 0, 0
    bg_effects = []
    for b in blocks:
        A, B, C, D = (get(df, b, f"gemma4-{x}") for x in "ABCD")
        bgB, bgD = get(df, b, "bg-B"), get(df, b, "bg-D")
        row = {"block": b, "complete": all(x is not None for x in (A, B, C, D, bgB, bgD))}
        if B is not None and D is not None:
            fast_b = t_rec(B) <= 30
            helps_rec = (t_rec(B) - t_rec(D) >= 5) or (
                (B.lost_service_D - D.lost_service_D) >= max(500, 0.10 * B.lost_service_D))
            keeps_work = D.n_correct >= B.n_correct - 1 and (D.n_incorrect + D.n_duplicate_effect) <= (
                B.n_incorrect + B.n_duplicate_effect)
            hurts = (D.n_correct < B.n_correct - 1) or (
                (D.lost_service_D - B.lost_service_D) >= max(500, 0.10 * B.lost_service_D))
            row.update(B_fast=fast_b, helps=bool(helps_rec and keeps_work), hurts=bool(hurts),
                       dT_agent=t_rec(D) - t_rec(B), dD_agent=D.lost_service_D - B.lost_service_D,
                       correct_B=int(B.n_correct), correct_D=int(D.n_correct), goals_B=int(B.goals),
                       goals_D=int(D.goals))
            n_unneeded += fast_b
            n_helps += row["helps"]
            n_hurts += row["hurts"]
        if A is not None and C is not None:
            row["healthy_p95_change"] = C.B_p95_ms / A.B_p95_ms - 1
            row["C_baseline_rejection_share"] = C.gate_baseline_rejection_share
            if row["healthy_p95_change"] > 0.05:
                n_hurts += 0  # counted once below as its own criterion
        if bgB is not None and bgD is not None:
            row.update(dT_bg=t_rec(bgD) - t_rec(bgB), dD_bg=bgD.lost_service_D - bgB.lost_service_D)
            bg_effects.append(row["dD_bg"])
        per_block.append(row)
    spread = (max(bg_effects) - min(bg_effects)) if len(bg_effects) > 1 else math.nan
    for row in per_block:
        if "dD_agent" in row and "dD_bg" in row and not math.isnan(spread):
            row["agent_specific"] = abs(row["dD_agent"] - row["dD_bg"]) > spread
            n_ai += row["agent_specific"]
    healthy_cost = sum(1 for r in per_block if r.get("healthy_p95_change", 0) > 0.05)
    judged = sum(1 for r in per_block if "B_fast" in r)
    verdict = {
        "blocks_judged": judged,
        "rule1_gate_unnecessary (B fast in >=2/3)": n_unneeded >= 2,
        "rule2_gate_helps (>=2/3)": n_helps >= 2,
        "rule3_gate_hurts (>=2/3, or healthy p95 +5% in >=2/3)": n_hurts >= 2 or healthy_cost >= 2,
        "rule5_agent_specific (>=2/3)": n_ai >= 2,
        "bg_dD_spread": spread,
    }
    if verdict["rule1_gate_unnecessary (B fast in >=2/3)"]:
        verdict["overall"] = "gate not needed in this regime (rule 1)"
    elif verdict["rule2_gate_helps (>=2/3)"] and not verdict["rule3_gate_hurts (>=2/3, or healthy p95 +5% in >=2/3)"]:
        verdict["overall"] = "gate helps (rule 2)"
    elif verdict["rule3_gate_hurts (>=2/3, or healthy p95 +5% in >=2/3)"]:
        verdict["overall"] = "gate hurts (rule 3)"
    else:
        verdict["overall"] = "inconclusive (rule 4)"
    print(json.dumps({"per_block": per_block, "verdict": verdict}, indent=2, default=float))


if __name__ == "__main__":
    main()
