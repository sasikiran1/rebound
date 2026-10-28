"""Runner CLI: `python -m harness <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from harness import dbadmin, probe, single


def cmd_clone(_args) -> None:
    fp = asyncio.run(dbadmin.clone_trial_db())
    rows = ", ".join(f"{t}={v['rows']}" for t, v in fp["tables"].items())
    print(f"cloned and verified: {fp['checksum']}\n{rows}")


def cmd_pulse(args) -> None:
    asyncio.run(probe.pulse(latency_ms=args.latency_ms, jitter_ms=args.jitter_ms, during_s=args.duration_s))


def cmd_endpoints(args) -> None:
    asyncio.run(probe.endpoints(n=args.n))


def cmd_verify_model(args) -> None:
    asyncio.run(single.verify_model(args.model))


def cmd_goal(args) -> None:
    asyncio.run(single.run_one(args.task, args.model))


def cmd_trial(args) -> None:
    from harness import trial
    from harness.manifest import load

    m = load(args.manifest)
    if args.condition == "agent":
        n = round(m.arrivals.goals_per_s * (m.arrivals.stop_s - m.arrivals.start_s))
        typical, cap = single.estimate_from_manifest(m)
        print(f"model spend estimate for ~{n} goals: ~${n * typical:.2f} typical "
              f"(per-trial cap ${m.limits.trial_budget_usd:.2f} cancels the trial)")
    out = trial.run(args.manifest, args.condition, args.gate, experiment=args.experiment, repeat=args.repeat,
                    block=args.block, label=args.label,
                    fault_enabled=False if args.no_fault else None, fault_duration_s=args.fault_duration_s)
    verdict = out / "verdict.json"
    if verdict.exists():
        v = json.loads(verdict.read_text())
        print(json.dumps({k: v[k] for k in ("trial_id", "status", "recovery", "goals", "background")}, indent=2,
                         default=str))
        print(f"model cost ${v['model'].get('cost_usd', 0):.3f}" if v.get("model") else "")
    print(f"trial bundle: {out}")


def cmd_analyze(args) -> None:
    from analysis.metrics import analyze
    from analysis.report import trial_report
    from harness.trial import verify_sums

    failed = 0
    for d in sorted(Path(args.run).glob("tr_*")) if not (Path(args.run) / "trial.json").exists() else [Path(args.run)]:
        bad = verify_sums(d) if (d / "SHA256SUMS").exists() else ["SHA256SUMS missing"]
        if bad:  # fail closed: never analyze a bundle whose recorded files do not match their sums
            print(f"{d}: INTEGRITY FAILURE {bad[:5]}; not analyzed")
            failed += 1
            continue
        out = d / "reanalysis"  # recorded files are never overwritten
        res = analyze(d, out)
        trial_report(d, res, out)
        print(f"{d}: {res['status']} recovery={res['recovery']['verdict']} T_rec={res['recovery']['t_rec_s']} "
              f"S={res['goals']['success_S']} D={res['background']['lost_service_D']:.0f} -> {out}")
    if failed:
        raise SystemExit(1)


def cmd_mark_crashed(args) -> None:
    """Clean up a trial whose runner died: keep every file, mark it INVALID with the reason,
    total its model spend from model_calls.jsonl, and write SHA256SUMS."""
    from harness.trial import sha256sums

    d = Path(args.trial)
    meta = json.loads((d / "trial.json").read_text())
    if meta.get("final_state"):
        raise SystemExit(f"{d} already ended in {meta['final_state']}")
    spent = 0.0
    calls = d / "model_calls.jsonl"
    for line in calls.read_text().splitlines() if calls.exists() else []:
        try:
            spent += json.loads(line).get("cost_usd") or 0.0
        except ValueError:  # a line cut off by the crash
            pass
    meta["invalid_reasons"].append(f"runner crashed: {args.reason}")
    meta["final_state"] = "INVALID"
    meta["spent_usd"] = round(spent, 4)
    meta["states"].append({"state": "INVALID", "reason": f"runner crashed: {args.reason}", "recovered": True})
    (d / "trial.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    sha256sums(d)
    print(f"{d}: INVALID ({args.reason}); model spend ${spent:.3f}")


def cmd_step_test(args) -> None:
    from harness import calibrate

    calibrate.run(args.manifest, [float(x) for x in args.rates.split(",")], args.step_s)


def cmd_verify_tasks(args) -> None:
    from harness import taskcheck

    asyncio.run(taskcheck.verify(args.tasks))


def main() -> None:
    p = argparse.ArgumentParser(prog="harness")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("clone", help="clone fielddesk_template into the trial DB and verify it").set_defaults(
        func=cmd_clone
    )
    sp = sub.add_parser("pulse", help="manual latency pulse with canary and FieldDesk DB time")
    sp.add_argument("--latency-ms", type=int, default=400)
    sp.add_argument("--jitter-ms", type=int, default=50)
    sp.add_argument("--duration-s", type=int, default=10)
    sp.set_defaults(func=cmd_pulse)
    sp = sub.add_parser("endpoints", help="per-endpoint DB time on the healthy system")
    sp.add_argument("--n", type=int, default=100)
    sp.set_defaults(func=cmd_endpoints)
    sub.add_parser("proxy-overhead", help="Toxiproxy overhead with no toxic (direct vs proxied queries)").set_defaults(
        func=lambda _a: asyncio.run(probe.proxy_overhead())
    )
    sp = sub.add_parser("verify-model", help="one minimal tool call plus one previous_response_id continuation")
    sp.add_argument("--model", default="muse-spark-1.3")
    sp.set_defaults(func=cmd_verify_model)
    sp = sub.add_parser("goal", help="run one task end to end with a live agent, then the oracle")
    sp.add_argument("--task", default=None, help="task ID (default: first one-day task)")
    sp.add_argument("--model", default="muse-spark-1.3")
    sp.set_defaults(func=cmd_goal)
    sp = sub.add_parser("trial", help="run one trial end to end")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--condition", required=True, choices=["agent", "scripted", "replay", "background"])
    sp.add_argument("--gate", required=True, choices=["on", "off"])
    sp.add_argument("--experiment", default=None)
    sp.add_argument("--repeat", type=int, default=None)
    sp.add_argument("--block", type=int, default=None)
    sp.add_argument("--label", default=None)
    sp.add_argument("--no-fault", action="store_true", help="healthy reference trial (no toxic)")
    sp.add_argument("--fault-duration-s", type=float, default=None, help="override, e.g. the unfamiliar 20 s fault")
    sp.set_defaults(func=cmd_trial)
    sp = sub.add_parser("analyze", help="re-run analysis and reports for a trial or experiment directory")
    sp.add_argument("run")
    sp.set_defaults(func=cmd_analyze)
    sp = sub.add_parser("mark-crashed", help="mark a trial whose runner died as INVALID, keeping all files")
    sp.add_argument("trial")
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_mark_crashed)
    sp = sub.add_parser("step-test", help="background-only capacity step test")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--rates", default="50,100,200,300,400,500,600,800")
    sp.add_argument("--step-s", type=float, default=40)
    sp.set_defaults(func=cmd_step_test)
    sp = sub.add_parser("verify-tasks", help="independent SQL check of a task file against the trial DB")
    sp.add_argument("tasks")
    sp.set_defaults(func=cmd_verify_tasks)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
