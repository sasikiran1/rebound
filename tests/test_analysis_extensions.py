"""Tests for the 2026-10-28 additions: zero-goal analysis, background condition, v2 tool parameters, cost estimate,
C5 acceptance and the campaign decision rules."""

import json
import math
import sys
from types import SimpleNamespace as NS

import pandas as pd

from analysis.metrics import goals_frame
from harness.adapter import GoalScope, build_request


def test_goals_frame_of_a_trial_with_no_goals_has_the_expected_columns():
    td = NS(client=[{"event_type": "trial.state", "goal_id": None}], oracle=[], t=lambda ns: 0.0, t_wall=lambda s: 0.0)
    df = goals_frame(td)
    assert len(df) == 0 and {"goal_id", "t_sched", "verdict"} <= set(df.columns)


async def test_background_condition_schedules_no_goals():
    from harness.trial import CONDITIONS, Trial
    assert "background" in CONDITIONS
    t = Trial.__new__(Trial)
    t.condition, t.meta, t.m = "background", {}, NS(arrivals=NS(goals_per_s=0.4, seed=7, start_s=0, stop_s=490))
    tasks: list = []
    await t.arrivals({}, tasks)
    assert t.meta["arrivals_planned"] == 0 and tasks == []


def test_v2_tool_arguments_pass_through_and_v1_schema_rejects_them():
    scope = GoalScope("tr", 1, 11, 101, ["R03"])
    _, _, _, params, _ = build_request("check_day_availability", {"region": "R03", "date": "2026-11-11",
                                                                  "service": "s", "start_after": "15:00",
                                                                  "offset": 40}, scope)
    assert params["start_after"] == "15:00" and params["offset"] == 40
    v1 = {t["name"]: t for t in json.load(open("prompts/tools_v1.json"))}
    v2 = {t["name"]: t for t in json.load(open("prompts/tools_v2_time.json"))}
    day = "check_day_availability"
    assert "start_after" not in v1[day]["parameters"]["properties"]
    assert v1[day]["parameters"]["additionalProperties"] is False
    assert {"start_after", "start_before"} <= set(v2[day]["parameters"]["properties"])


def test_cost_estimate_matches_the_measured_eval_mix():
    from harness import single
    from harness.agent import AgentConfig
    typical, cap = single.estimate(AgentConfig(model="muse-spark-1.3"))
    assert 0.009 < typical < 0.012 and cap > typical


def test_c5_fails_when_the_unfamiliar_fault_loses_useful_work():
    # eval-v1 tr_0017 / tr_0018: recovery faster and less lost service, but 8 fewer correct goals
    from analysis.experiment import c5
    m = {"t_rec_off": 34.0, "t_rec_on": 10.0, "all_fast_off": False, "all_fast_on": True, "correct_off": 209,
         "correct_on": 201, "incorrect_off": 0, "incorrect_on": 0, "duplicate_off": 0, "duplicate_on": 0,
         "D_off": 19491.0, "D_on": 10212.0}
    r = c5({"fault_20s": {"unpaired_means": m}})
    assert r["verdict"] == "fail" and r["directions"]["fault_20s"] == {"recovery": True, "useful_work": False,
                                                                      "ordinary_users": True}
    assert c5({})["verdict"] == "insufficient_evidence"


def test_summarize_judges_only_the_named_fingerprint(tmp_path):
    from analysis.experiment import summarize
    for i, fp in enumerate(["sha256:aaa", "sha256:aaa", "sha256:bbb"], 1):
        d = tmp_path / f"tr_{i:04d}"
        d.mkdir()
        (d / "trial.json").write_text(json.dumps({"final_state": "COMPLETE", "block": 1, "label": "x"}))
        (d / "verdict.json").write_text(json.dumps({"trial_id": d.name, "status": "valid", "condition": "agent",
                                                    "gate": "off", "config_hash": fp, "compat_fingerprint": fp,
                                                    "fault": {"duration_s": 10, "enabled": True}}))
    mixed = summarize(tmp_path)
    assert mixed["acceptance"]["overall"] == "insufficient_evidence" and mixed["problems"]
    one = summarize(tmp_path, fingerprint="sha256:aaa")
    assert not one["problems"] and [e["trial_id"] for e in one["excluded"]] == ["tr_0003"]


def test_decision_rules_put_gate_not_needed_first(tmp_path, capsys, monkeypatch):
    from analysis import ollama_decision
    rows = []
    for b in (1, 2, 3):
        for label, t_rec, D, correct in (("gemma4-A", 0, 3, 18), ("gemma4-B", 20, 8000, 18), ("gemma4-C", 1, 80, 18),
                                         ("gemma4-D", 10, 5000, 18), ("bg-B", 20, 7900, 0), ("bg-D", 10, 5000, 0)):
            rows.append({"block": b, "label": label, "status": "valid", "t_rec_s": t_rec, "lost_service_D": D,
                         "n_correct": correct, "n_incorrect": 0, "n_duplicate_effect": 0, "goals": correct,
                         "B_p95_ms": 7.0, "gate_baseline_rejection_share": 0.0})
    rows.append({**rows[1], "status": "invalid"})  # an invalid original beside its valid rerun is ignored
    p = tmp_path / "c.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    monkeypatch.setattr(sys, "argv", ["x", str(p)])
    ollama_decision.main()
    out = json.loads(capsys.readouterr().out)
    v = out["verdict"]
    assert v["blocks_judged"] == 3 and v["overall"].startswith("gate not needed")
    assert v["rule2_gate_helps (>=2/3)"] is True and not math.isnan(v["bg_dD_spread"])
