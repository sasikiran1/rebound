"""Regression tests for evidence integrity (audit 2026-10-27, findings 2-5).

Each test encodes one rule the experiment summary or the analysis CLI must enforce:
- never pool trials whose compatibility fingerprints differ;
- a raw-evidence checksum failure must block a result, not just print a warning;
- decision-making code (oracle, analysis, schema, generator, protocol, lockfile) is part of provenance;
- missing evidence can never pass an acceptance criterion, and all five criteria get explicit verdicts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis import experiment


def verdict(trial_id, condition, gate, block, *, config_hash="sha256:aaa", fault_s=10, t_rec=10.0, rec="fast",
            correct=220, scheduled=228, d=5000.0, baseline_s=1.0, bg_p95=8.0, rej=0.0, incorrect=0, duplicate=0,
            status="valid"):
    return {
        "trial_id": trial_id, "condition": condition, "gate": gate, "block": block, "label": None,
        "status": status, "config_hash": config_hash,
        "fault": {"duration_s": fault_s, "enabled": True},
        "recovery": {"t_rec_s": t_rec, "verdict": rec},
        "goals": {"scheduled": scheduled, "success_S": correct / scheduled,
                  "verdicts": {"correct": correct, "incorrect": incorrect, "duplicate_effect": duplicate}},
        "background": {"lost_service_D": d, "baseline_p95_ms": bg_p95, "baseline_success": 1.0},
        "cohorts": [{"cohort": "baseline", "success": baseline_s}],
        "gate_stats": {"baseline_rejection_share": rej},
        "invalid_reasons": [], "inconclusive_reasons": [],
    }


def write_exp(tmp_path: Path, verdicts: list[dict]) -> Path:
    for v in verdicts:
        d = tmp_path / v["trial_id"]
        d.mkdir()
        (d / "verdict.json").write_text(json.dumps(v))
        (d / "trial.json").write_text(json.dumps({"final_state": "COMPLETE", "block": v["block"]}))
    return tmp_path


def test_summary_never_pools_different_configurations(tmp_path):
    exp = write_exp(tmp_path, [
        verdict("tr_0001", "agent", "off", 1, config_hash="sha256:old", t_rec=20.0),
        verdict("tr_0002", "agent", "off", 1, config_hash="sha256:new", t_rec=10.0),
    ])
    s = experiment.summarize(exp)
    agent_off = [c for c in s["cells"] if c["condition"] == "agent" and c["gate"] == "off"]
    assert all(c["n"] == 1 for c in agent_off), "trials with different config hashes were pooled into one cell"
    assert s["mixed_configurations"] is True


def test_acceptance_missing_baseline_is_insufficient_not_pass(tmp_path):
    vs = []
    for b in (1, 2, 3):
        vs.append(verdict(f"tr_off{b}", "agent", "off", b, baseline_s=None, bg_p95=None, rej=None))
        vs.append(verdict(f"tr_on{b}", "agent", "on", b, baseline_s=None, bg_p95=None, rej=None))
    s = experiment.summarize(write_exp(tmp_path, vs))
    c1 = s["acceptance"]["fault_10s"]["c1_healthy_overhead"]
    assert c1["verdict"] == "insufficient_evidence"
    assert c1["pass"] is not True


def test_acceptance_duplicate_arm_in_block_is_not_hidden(tmp_path):
    vs = [verdict("tr_a", "agent", "off", 1), verdict("tr_b", "agent", "off", 1, t_rec=200.0, rec="slow"),
          verdict("tr_c", "agent", "on", 1)]
    s = experiment.summarize(write_exp(tmp_path, vs))
    acc = s["acceptance"]["fault_10s"]
    assert any("duplicate" in r for r in acc["problems"])
    assert acc["c2_recovery"]["verdict"] == "insufficient_evidence"


def test_acceptance_reports_all_five_criteria_and_overall(tmp_path):
    vs = []
    for b in (1, 2, 3):
        vs += [verdict(f"tr_off{b}", "agent", "off", b, t_rec=20.0, d=7000.0),
               verdict(f"tr_on{b}", "agent", "on", b, t_rec=10.0, d=5000.0)]
    vs += [verdict("tr_u_off", "agent", "off", None, fault_s=20, config_hash="sha256:aaa", t_rec=40.0, rec="slow",
                   d=9000.0),
           verdict("tr_u_on", "agent", "on", None, fault_s=20, config_hash="sha256:aaa", t_rec=200.0, rec="slow",
                   d=9500.0)]
    s = experiment.summarize(write_exp(tmp_path, vs))
    acc = s["acceptance"]
    crit = acc["criteria"]
    assert set(crit) == {"c1_healthy_overhead", "c2_recovery", "c3_useful_work", "c4_ordinary_users",
                         "c5_unfamiliar_fault"}
    assert all(c["verdict"] in ("pass", "fail", "insufficient_evidence") for c in crit.values())
    # The 20 s fault reverses the recovery direction (gate-on slower): c5 must fail, so the overall verdict fails.
    assert crit["c5_unfamiliar_fault"]["verdict"] == "fail"
    assert acc["overall"] == "fail"


def test_config_hash_covers_decision_making_code(tmp_path, monkeypatch):
    from harness import manifest

    for rel, text in {
        "prompts/p.md": "prompt", "prompts/t.json": "[]", "harness/a.py": "x = 1", "backend/b.py": "y = 1",
        "oracle/verdicts.py": "RULE = 1", "analysis/metrics.py": "M = 1", "data/schema.sql": "create table t();",
        "data/generator/generate.py": "G = 1", "docs/protocol.md": "rules", "uv.lock": "lock",
        "docker/postgres.conf": "x",
    }.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    monkeypatch.setattr(manifest, "ROOT", tmp_path)
    resolved = {"model": {"prompt": "prompts/p.md", "tools": "prompts/t.json"}, "backend": {"images": {}},
                "data": {"checksum": "c"}, "tasks": {"sha256": "t"}}
    before = manifest.config_hash(resolved)
    (tmp_path / "oracle/verdicts.py").write_text("RULE = 2")
    assert manifest.config_hash(resolved) != before, "changing the oracle did not change the configuration identity"
    after_oracle = manifest.config_hash(resolved)
    (tmp_path / "data/schema.sql").write_text("create table t(x int);")
    assert manifest.config_hash(resolved) != after_oracle, "changing the schema did not change the identity"


def test_analyze_cli_fails_closed_on_corrupt_raw_evidence(tmp_path, monkeypatch, capsys):
    from harness import __main__ as cli
    from harness.trial import sha256sums

    d = tmp_path / "tr_0001"
    d.mkdir()
    (d / "trial.json").write_text(json.dumps({"trial_id": "tr_0001"}))
    (d / "backend.jsonl").write_text('{"a": 1}\n')
    (d / "verdict.json").write_text(json.dumps({"status": "valid"}))
    sha256sums(d)
    (d / "backend.jsonl").write_text('{"a": 2}\n')  # tamper with raw evidence after hashing

    called = []
    import analysis.metrics

    monkeypatch.setattr(analysis.metrics, "analyze", lambda *a, **k: called.append(a) or {})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_analyze(type("A", (), {"run": str(d)})())
    assert exc.value.code != 0
    assert not called, "analysis ran on evidence whose checksum failed"
    assert json.loads((d / "verdict.json").read_text()) == {"status": "valid"}  # the saved verdict is untouched
