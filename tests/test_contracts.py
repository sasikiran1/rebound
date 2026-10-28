"""Recovery contract: false PASS and false FAIL controls (audit section 10, last rows)."""

from analysis.contracts import FAIL, INSUFFICIENT, PASS, compare, trial_contract


def v(status="valid", correct=228, scheduled=228, incorrect=0, followed=10, violations=0, r_base=4.0, r_fault=5.0,
      block=None, t_rec=10.0, d=5000.0, fp="sha256:x", trial_id="tr"):
    verdicts = {"correct": correct}
    if incorrect:
        verdicts["incorrect"] = incorrect
    if scheduled - correct - incorrect:
        verdicts["failed"] = scheduled - correct - incorrect
    return {"trial_id": trial_id, "status": status, "block": block, "compat_fingerprint": fp,
            "invalid_reasons": [], "inconclusive_reasons": [],
            "goals": {"scheduled": scheduled, "verdicts": verdicts},
            "backpressure": {"signals": followed, "followed_by_attempt": followed, "violations": violations,
                             "goals_with_violation": 1 if violations else 0,
                             "witnesses": [{"goal_id": "g_0078"}] if violations else []},
            "cohorts": [{"cohort": "baseline", "R_attempts_per_goal": r_base},
                        {"cohort": "fault", "R_attempts_per_goal": r_fault}],
            "recovery": {"t_rec_s": t_rec}, "background": {"lost_service_D": d}}


def test_clean_trial_passes():
    c = trial_contract(v(), integrity_ok=True)
    assert c["overall"] == PASS and all(p["verdict"] == PASS for p in c["predicates"].values())


def test_corrupt_evidence_never_passes():
    c = trial_contract(v(), integrity_ok=False)
    assert c["overall"] == INSUFFICIENT and c["predicates"]["business"]["verdict"] == INSUFFICIENT


def test_invalid_trial_is_not_judged():
    assert trial_contract(v(status="invalid"), integrity_ok=True)["overall"] == INSUFFICIENT


def test_known_violation_is_detected_with_witness():
    c = trial_contract(v(violations=3), integrity_ok=True)
    bp = c["predicates"]["backpressure"]
    assert bp["verdict"] == FAIL and bp["witnesses"][0]["goal_id"] == "g_0078" and c["overall"] == FAIL


def test_unexercised_backpressure_is_insufficient_not_pass():
    c = trial_contract(v(followed=0), integrity_ok=True)
    assert c["predicates"]["backpressure"]["verdict"] == INSUFFICIENT and c["overall"] == INSUFFICIENT


def test_business_failures_fail_the_contract():
    assert trial_contract(v(incorrect=1, correct=227), integrity_ok=True)["predicates"]["business"]["verdict"] == FAIL
    assert trial_contract(v(correct=200), integrity_ok=True)["predicates"]["business"]["verdict"] == FAIL


def test_amplification_bound():
    assert trial_contract(v(r_base=4.0, r_fault=9.0), integrity_ok=True)["predicates"]["amplification"][
        "verdict"] == FAIL


def test_compare_needs_repeated_controls_and_blocks():
    t = [v(block=1, t_rec=60.0, trial_id="t1")]
    c = [v(block=1, t_rec=10.0, trial_id="c1")]
    assert compare(t, c)["metrics"]["t_rec_s"]["verdict"] == INSUFFICIENT


def test_compare_material_effect_must_exceed_control_spread():
    t = [v(block=b, t_rec=40.0, trial_id=f"t{b}") for b in (1, 2, 3)]
    c = [v(block=1, t_rec=10.0, trial_id="c1"), v(block=2, t_rec=12.0, trial_id="c2"),
         v(block=3, t_rec=11.0, trial_id="c3")]
    assert compare(t, c)["metrics"]["t_rec_s"]["verdict"] == "MATERIAL_ADVERSE_EFFECT"
    noisy = [v(block=1, t_rec=10.0, trial_id="c1"), v(block=2, t_rec=45.0, trial_id="c2"),
             v(block=3, t_rec=11.0, trial_id="c3")]
    assert compare(t, noisy)["metrics"]["t_rec_s"]["verdict"] == "NO_MATERIAL_ADVERSE_EFFECT"


def test_compare_refuses_mixed_configurations():
    t = [v(block=b, fp="sha256:a", trial_id=f"t{b}") for b in (1, 2, 3)]
    c = [v(block=b, fp="sha256:b", trial_id=f"c{b}") for b in (1, 2, 3)]
    r = compare(t, c)
    assert r["problems"] and all(m["verdict"] == INSUFFICIENT for m in r["metrics"].values())


def test_screening_decision_supports_h1_and_h2_only_with_two_blocks():
    from analysis.screening import INSUFFICIENT, SUPPORTED, decide

    def trial(arm, block, viol_share=0.0, followed=10, rejected=10, correct=227, f60=0.99):
        x = v(block=block, followed=followed, violations=int(viol_share * followed), correct=correct)
        x.update(arm=arm, backpressure={**x["backpressure"], "violation_share": viol_share},
                 exposure={"fault": {"goal_requests_rejected": rejected, "goal_share_inflight": 0.01},
                           "post60": {"goal_requests_rejected": 0}},
                 cohorts=[{"cohort": "baseline", "R_attempts_per_goal": 4.0, "goals": 40},
                          {"cohort": "fault", "R_attempts_per_goal": 5.0, "goals": 40}])
        x["goals"]["F_60s"] = f60
        return x

    rows = []
    for b in (1, 2):
        rows += [trial("agent", b, viol_share=1.0, rejected=80), trial("agent-guard", b, rejected=20),
                 trial("scripted-paced", b), trial("scripted-unpaced", b)]
    res = decide(rows)
    assert res["H1_backpressure_violation"]["verdict"] == SUPPORTED
    assert res["H2_guard_preserves_useful_work"]["verdict"] == SUPPORTED
    assert decide(rows[:4])["H1_backpressure_violation"]["verdict"] == INSUFFICIENT  # one block is not enough
