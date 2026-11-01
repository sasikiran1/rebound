"""Replay reference provenance and recorded pacing samples (audit findings 7 and 8)."""

import json

import pytest

from harness.manifest import load
from harness.trial import TrialAbort, replay_reference, step_gap_samples


def write_ref(d, *, condition="agent", fault=False, final="COMPLETE", tasks_sha=None, seed=7):
    m = load("manifests/exploratory/screen_v2.yaml")
    d.mkdir(parents=True)
    (d / "trial.json").write_text(json.dumps({"trial_id": "tr_0001", "condition": condition, "final_state": final,
                                              "t0_mono_ns": 0}))
    (d / "manifest.resolved.json").write_text(json.dumps({
        "fault_enabled": fault, "tasks": {"sha256": tasks_sha or m.tasks.sha256}, "arrivals": {"seed": seed}}))
    (d / "client.jsonl").write_text("")
    return m


def test_healthy_agent_reference_has_no_problems(tmp_path):
    m = write_ref(tmp_path / "ref")
    assert replay_reference(tmp_path / "ref" / "client.jsonl", m)["problems"] == []


@pytest.mark.parametrize("kw, words", [
    ({"fault": True}, "fault enabled"), ({"condition": "scripted"}, "not a live agent"),
    ({"final": "INVALID"}, "ended"), ({"tasks_sha": "other"}, "task file"), ({"seed": 8}, "arrival seed"),
])
def test_unsuitable_reference_is_reported(tmp_path, kw, words):
    m = write_ref(tmp_path / "ref", **kw)
    assert any(words in p for p in replay_reference(tmp_path / "ref" / "client.jsonl", m)["problems"])


def test_step_gap_samples_use_baseline_cohort_agent_ops(tmp_path):
    d = tmp_path / "ref"
    d.mkdir()
    (d / "trial.json").write_text(json.dumps({"t0_mono_ns": 0}))
    ev = [
        {"actor_kind": "agent", "goal_id": "g_1", "event_type": "goal.scheduled", "ts_mono_ns": int(70e9)},
        {"actor_kind": "agent", "goal_id": "g_1", "event_type": "tool.op", "status": "ok", "ts_mono_ns": int(71e9)},
        {"actor_kind": "agent", "goal_id": "g_1", "event_type": "tool.op", "status": "ok", "ts_mono_ns": int(72.5e9)},
        {"actor_kind": "agent", "goal_id": "g_2", "event_type": "goal.scheduled", "ts_mono_ns": int(200e9)},
        {"actor_kind": "agent", "goal_id": "g_2", "event_type": "tool.op", "status": "ok", "ts_mono_ns": int(201e9)},
        {"actor_kind": "agent", "goal_id": "g_2", "event_type": "tool.op", "status": "ok", "ts_mono_ns": int(209e9)},
    ]
    (d / "client.jsonl").write_text("\n".join(json.dumps(e) for e in ev) + "\n")
    assert step_gap_samples(d / "client.jsonl") == pytest.approx([1.5])  # g_2 arrived outside the baseline cohort
    (d / "client.jsonl").write_text("")
    with pytest.raises(TrialAbort):
        step_gap_samples(d / "client.jsonl")
