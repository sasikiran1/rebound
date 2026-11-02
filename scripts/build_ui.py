"""Build ui/index.html: a static, read-only results page generated only from recorded evidence.

Usage: python scripts/build_ui.py
Reads runs/eval-v1 (bundles, re-analysis, summary), runs/ollama-recovery-v1 (background-only control) and
runs/ollama-semantic/sem_tools_v2 (tool test), verifies each eval bundle's raw checksums, and embeds the numbers in
ui/template.html. No number is typed by hand; a trial that fails its checksums is shown as such. No server, no
network requests: the output opens from disk.
"""

import csv
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")
from harness.trial import verify_raw  # noqa: E402

EVAL = Path("runs/eval-v1")
WINDOW = (150, 330)  # seconds of each trial's per-second series shown in the recovery chart


def num(x):
    return None if x in ("", None) else round(float(x), 4)


def load_verdict(d: Path) -> dict:
    p = d / "reanalysis" / "verdict.json"
    return json.loads((p if p.exists() else d / "verdict.json").read_text())


def series(d: Path) -> dict:
    out = {"t": [], "success": [], "p95": [], "inflight": [], "healthy": []}
    with (d / "per_second.csv").open() as f:
        for r in csv.DictReader(f):
            t = int(float(r["t"]))
            if WINDOW[0] <= t <= WINDOW[1]:
                out["t"].append(t)
                out["success"].append(num(r["bg_success"]))
                out["p95"].append(num(r["bg_p95_ms"]))
                out["inflight"].append(num(r["in_flight"]))
                out["healthy"].append(r["healthy"] == "True")
    return out


def eval_trials() -> tuple[list, dict]:
    trials, all_series = [], {}
    for d in sorted(EVAL.glob("tr_*")):
        meta = json.loads((d / "trial.json").read_text())
        v = load_verdict(d)
        bp = v.get("backpressure") or {}
        expo = (v.get("exposure") or {}).get("fault") or {}
        trials.append({
            "id": d.name, "label": meta["label"], "condition": meta["condition"], "gate": meta["gate"],
            "block": meta["block"], "fault_on": v["fault"]["enabled"], "fault_s": v["fault"]["duration_s"],
            "status": v["status"], "sums_ok": not verify_raw(d), "t_rec": v["recovery"]["t_rec_s"],
            "recovery": v["recovery"]["verdict"], "lost": round(v["background"]["lost_service_D"]),
            "goals": v["goals"]["scheduled"], "verdicts": v["goals"]["verdicts"],
            "correct": v["goals"]["verdicts"].get("correct", 0), "cost": meta.get("spent_usd") or 0,
            "canary": v["fault"].get("canary_rise_ms"), "signals": bp.get("signals", 0),
            "too_early": bp.get("violations", 0), "followed": bp.get("followed_by_attempt", 0),
            "goal_share": expo.get("goal_share_inflight"), "gap_p50": bp.get("gap_s_p50"),
        })
        all_series[d.name] = series(d)
    return trials, all_series


def goal_replay(trial: str) -> dict:
    """The lost goal with the most gate refusals: its real HTTP attempts and visible model text, in order."""
    d = EVAL / trial
    t0 = json.loads((d / "trial.json").read_text())["t0_mono_ns"]
    with (d / "goals.csv").open() as f:
        lost = [r for r in csv.DictReader(f) if r["verdict"] == "failed"]
    goal = max(lost, key=lambda r: float(r["gate_rejections"] or 0))
    gid = goal["goal_id"]
    events = []
    for name in ("client.jsonl", "model_calls.jsonl"):
        p = d / name
        for line in p.read_text().splitlines() if p.exists() else []:
            e = json.loads(line)
            if e.get("goal_id") == gid:
                events.append(e)
    events.sort(key=lambda e: e["ts_mono_ns"])
    steps = []
    for e in events:
        t = round((e["ts_mono_ns"] - t0) / 1e9, 2)
        if e["event_type"] == "http.attempt":
            steps.append({"t": t, "kind": "http", "tool": e.get("tool"), "status": e.get("http_status"),
                          "outcome": e.get("status"), "retry_after": e.get("retry_after_s")})
        elif e["event_type"] == "agent.message" and (e.get("text") or "").strip():
            steps.append({"t": t, "kind": "text", "text": e["text"].strip()[:280]})
    return {"trial": trial, "goal": gid, "task": goal["task_id"], "verdict": goal["verdict"],
            "end_reason": goal["end_reason"], "limit": goal["limit"], "refusals": int(float(goal["gate_rejections"])),
            "steps": steps}


def background_control() -> list:
    rows = []
    for d in sorted(Path("runs/ollama-recovery-v1").glob("tr_*")):
        meta = json.loads((d / "trial.json").read_text())
        if meta["condition"] != "background":
            continue
        v_path = d / "reanalysis" / "verdict.json"
        if not v_path.exists() and not (d / "verdict.json").exists():
            continue
        v = load_verdict(d)
        if v["status"] == "valid":
            rows.append({"id": d.name, "block": meta["block"], "gate": meta["gate"], "t_rec": v["recovery"]["t_rec_s"],
                         "lost": round(v["background"]["lost_service_D"])})
    return rows


def tool_test() -> list:
    rows = json.loads(Path("runs/ollama-semantic/sem_tools_v2/results.json").read_text())
    out = []
    for r in rows:
        s = r["scenario"]
        out.append({"model": r["model"].split(":")[0].replace("-16k", "").replace("-8k", ""), "arm": r["setting"],
                    "case": r["case"], "class": s["class"], "booked": (s["booked"] or {}).get("starts_at", "")[11:16],
                    "visible": len(s["satisfying_slots_visible_to_model"]), "old_oracle": r["original_oracle"],
                    "tools": r["tool_calls"]})
    return out


def guard_study() -> list:
    """Each guard trial beside its eval-v1 comparator (same manifest, gate on, different code)."""
    rows = []
    pairs = (("tr_0004", "tr_0001", "10 s · block 1"), ("tr_0010", "tr_0002", "10 s · block 2"),
             ("tr_0016", "tr_0003", "10 s · block 3"), ("tr_0018", "tr_0004", "20 s"))
    for base, guarded, name in pairs:
        arms = {}
        for key, d in (("off", EVAL / base), ("on", Path("runs/agent-v2-guard") / guarded)):
            v = load_verdict(d)
            bp = v["backpressure"]
            arms[key] = {"id": d.name, "sums_ok": not verify_raw(d), "status": v["status"],
                         "correct": v["goals"]["verdicts"].get("correct", 0), "goals": v["goals"]["scheduled"],
                         "too_early": bp["violations"], "followed": bp["followed_by_attempt"],
                         "signals": bp["signals"], "gap_p50": bp["gap_s_p50"], "t_rec": v["recovery"]["t_rec_s"],
                         "lost": round(v["background"]["lost_service_D"])}
        rows.append({"name": name, **arms})
    return rows


def agent_v2() -> dict:
    def arm_counts(path: str) -> dict:
        rows = json.loads(Path(path).read_text())
        return {a: {"n": sum(r["arm"] == a for r in rows),
                    "correct": sum(r["arm"] == a and r["verdict"] == "correct" for r in rows),
                    "incorrect": sum(r["arm"] == a and r["verdict"] == "incorrect" for r in rows),
                    "failed": sum(r["arm"] == a and r["verdict"] == "failed" for r in rows)}
                for a in ("original", "hardened")}

    held = json.loads(Path("runs/agent-v2/muse_held_out/results.json").read_text())
    tools = {a: [r["tool_calls"] for r in held if r["arm"] == a and r["kind"] == "afternoon"]
             for a in ("original", "hardened")}
    hunt = json.loads(Path("runs/agent-v2/muse_hunt_2/findings.json").read_text())
    scenarios = [json.loads(x) for x in Path("runs/agent-v2/muse_hunt_2/scenarios.jsonl").read_text().splitlines()]
    return {"muse": arm_counts("runs/agent-v2/muse_held_out/results.json"),
            "local": arm_counts("runs/agent-v2/local_dev_1/results.json"), "afternoon_tools": tools,
            "hunt": {"n": len(scenarios), "verdicts": hunt["verdicts"], "candidates": len(hunt["candidates"]),
                     "infeasible": sum(s["feasible"] == 0 for s in scenarios),
                     "kinds": dict(Counter(s["constraint"]["kind"] for s in scenarios))}}


def main() -> None:
    trials, all_series = eval_trials()
    summary = json.loads((EVAL / "summary.json").read_text())
    data = {
        "commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                                 text=True).stdout.strip(),
        "trials": trials, "series": all_series, "window": WINDOW,
        "criteria": {k: {"verdict": v.get("verdict")} for k, v in summary["acceptance"]["criteria"].items()},
        "excluded": summary["excluded"], "amplification": summary["amplification_vs_replay"],
        "replay": goal_replay("tr_0018"), "background": background_control(), "tools": tool_test(),
        "guard": guard_study(), "v2": agent_v2(),
        "spend": round(sum(t["cost"] for t in trials), 2),
        "outcomes": dict(sum((Counter(t["verdicts"]) for t in trials), Counter())),
    }
    template = Path("ui/template.html").read_text()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    Path("ui/index.html").write_text(template.replace("/*__DATA__*/null", payload))
    print(f"ui/index.html: {len(trials)} trials, {Path('ui/index.html').stat().st_size / 1024:.0f} KB, "
          f"all sums ok: {all(t['sums_ok'] for t in trials)}, spend ${data['spend']}")
    print("replay", data["replay"]["trial"], data["replay"]["goal"], data["replay"]["refusals"], "refusals,",
          len(data["replay"]["steps"]), "steps; background control trials:", len(data["background"]))


if __name__ == "__main__":
    main()
