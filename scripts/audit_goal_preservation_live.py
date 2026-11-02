"""Read-only audit and descriptive report for the fixed local-agent cohort."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from oracle.goal_preservation import optimum


def audit(root):
    freeze = json.loads((root / "freeze.json").read_text())
    plan = freeze["plan"]
    failures = []
    outcomes = []
    by_fixture = defaultdict(lambda: {"sessions": 0, "completed": 0, "batches": 0, "lost": 0})
    recovery_file = root / "source_recovery.json"
    recovery = json.loads(recovery_file.read_text()) if recovery_file.exists() else {}
    source_checks = {}
    for name, digest in freeze["source_hashes"].items():
        candidate = root / recovery.get("files", {}).get(
            name, freeze.get("archive_paths", {}).get(name, str(Path("source") / Path(name).name))
        )
        source_checks[name] = candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest
    if not all(source_checks.values()):
        failures.append("frozen source snapshot mismatch")
    if hashlib.sha256((root / "protocol.md").read_bytes()).hexdigest() != freeze["protocol_sha256"]:
        failures.append("protocol mismatch")
    first_calls = {}
    protected_commits = 0
    for n, cell in enumerate(plan, 1):
        directory = root / f"batch_{n:03d}"
        file = directory / "result.json"
        if not file.exists():
            failures.append(f"missing batch {n}")
            continue
        result = json.loads(file.read_text())
        if any(result[k] != v for k, v in cell.items()):
            failures.append(f"plan mismatch batch {n}")
        with sqlite3.connect(directory / "state.sqlite") as conn:
            goals = {g: set(json.loads(a)) for g, a in conn.execute("SELECT id,allowed FROM goals")}
            sql = list(conn.execute("SELECT goal_id,slot_id FROM bookings"))
        used = set()
        committed = set()
        pending = dict(goals)
        records = {}
        for s in result["sessions"]:
            outcomes.append(s)
            records[s["goal_id"]] = json.loads((directory / s["goal_id"] / "session.json").read_text())
            actual = any(g == s["goal_id"] for g, _ in sql)
            if actual != s["sql_completed"]:
                failures.append(f"SQL/session mismatch batch {n} {s['goal_id']}")
            raw = directory / s["goal_id"] / "model" / "call_00001.json"
            if raw.exists():
                request = json.loads(raw.read_text())["request"]
                key = (cell["model"], cell["fixture"], cell["order"], cell["seed"], s["goal_id"])
                if key in first_calls and first_calls[key] != request:
                    failures.append(f"unmatched first request batch {n}")
                first_calls[key] = request
            for event in records[s["goal_id"]]["trace"]:
                response = event["result"]
                if event["tool"] != "commit" or response.get("status") != 200:
                    continue
                body = response["body"]
                if body.get("replayed"):
                    continue
                g = s["goal_id"]
                slot = json.loads(event["arguments"])["slot_id"]
                current = {k: choices - used for k, choices in pending.items()}
                if g not in current or slot not in current[g]:
                    failures.append(f"invalid committed choice batch {n}")
                    continue
                after = {k: choices - {slot} for k, choices in current.items() if k != g}
                if cell["arm"] == "preservation":
                    protected_commits += 1
                    if 1 + optimum(after) < optimum(current):
                        failures.append(f"protected commit loses opportunity batch {n}")
                used.add(slot)
                committed.add((g, slot))
                del pending[g]
        if set(sql) != committed or len(sql) != len(used) or len({g for g, _ in sql}) != len(sql):
            failures.append(f"SQL/trace effect mismatch batch {n}")
        graph = {g: choices - {s for _, s in sql} for g, choices in goals.items() if g not in {k for k, _ in sql}}
        expected_lost = optimum(goals) - len(sql) - optimum(graph)
        if result["oracle"]["completed"] != len(sql) or result["oracle"]["avoidable_loss"] != expected_lost:
            failures.append(f"oracle mismatch batch {n}")
        v = by_fixture[(cell["model"], cell["fixture"], cell["arm"])]
        v["sessions"] += len(result["sessions"])
        v["completed"] += len(sql)
        v["batches"] += 1
        v["lost"] += expected_lost
    summary = json.loads((root / "summary.json").read_text())
    paired = defaultdict(list)
    for p in summary["paired_batches"]:
        paired[(p["model"], p["fixture"])].append(p["completion_difference"])
    benefits = {}
    for model in freeze["models"]:
        scarcity = paired[(model, "avoidable_contention")]
        control = paired[(model, "no_contention")]
        benefits[model] = {
            "contention_paired_sum": sum(scarcity),
            "control_paired_sum": sum(control),
            "criterion_supported": sum(scarcity) > 0 and sum(control) >= 0,
        }
    report = {
        "batches_planned": len(plan),
        "sessions_retained": len(outcomes),
        "failures": failures,
        "source_snapshot_checks": source_checks,
        "source_recovery": recovery,
        "protected_commits_exercised": protected_commits,
        "ends": dict(Counter(s["end"] for s in outcomes)),
        "benefit_criterion": benefits,
        "cells": [{"model": m, "fixture": f, "arm": a, **v} for (m, f, a), v in sorted(by_fixture.items())],
    }
    return report, summary


def write_report(root):
    report, summary = audit(root)
    (root / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Frozen local-agent feasibility cohort",
        "",
        "Constructed tasks; live local inference; real SQLite writes.",
        "HTTP transport was in-process ASGI, not a network outage test. All sessions retained.",
        "",
        "| Model | Condition | SQL completed / sessions | Avoidable losses | Uncompleted sessions |",
        "|---|---|---:|---:|---:|",
    ]
    for c in summary["cells"]:
        lines.append(
            f"| {c['model']} | {c['arm']} | {c['completed']}/{c['sessions']} | "
            f"{c['avoidable_loss']} | {c['unfinished_or_error_sessions']} |"
        )
    scheduler = json.loads((root / "scheduler.json").read_text())
    lines += ["", "Central constrained-first scheduler reference:", ""]
    for name, verdict in scheduler.items():
        lines.append(f"- {name}: {verdict['completed']}/2 completed; {verdict['avoidable_loss']} avoidable losses.")
    lines += [
        "",
        "These are deterministic reference outcomes, not live-model sessions.",
        "",
        "## Per-fixture results",
        "",
        "| Model | Fixture | Condition | Completed / sessions |",
        "|---|---|---|---:|",
    ]
    for c in report["cells"]:
        lines.append(f"| {c['model']} | {c['fixture']} | {c['arm']} | {c['completed']}/{c['sessions']} |")
    lines += [
        "",
        "## Fixed benefit criterion",
        "",
        "```json",
        json.dumps(report["benefit_criterion"], indent=2),
        "```",
        "",
        "A zero total difference does not support completion benefit. No significance or production-rate claim.",
        "An unexercised protection cannot demonstrate efficacy. Zero avoidable loss can coexist with zero work.",
        "",
        "## Latency and workflow",
        "",
    ]
    for c in summary["cells"]:
        lines.append(
            f"- {c['model']}/{c['arm']}: session median/min/max seconds "
            f"{c['wall_median_range_s']}; decision median/min/max seconds {c['decision_median_range_s']}; "
            f"recovered sessions {c['recovered_sessions']}."
        )
    lines += [
        "",
        f"Session endings: {report['ends']}.",
        f"Protected commits actually exercised: {report['protected_commits_exercised']}.",
        f"Audit failures: {report['failures']}.",
        "",
        "Independent audit checks SQL, trace effects, protected commit feasibility and source hashes.",
        "One source copy was recovered after collection against its frozen hash; see deviations.md.",
        "Plan coverage and paired first requests are checked. All raw calls and databases are retained.",
        "",
        "This small cohort does not validate natural-language extraction, FieldDesk integration,",
        "privacy, novelty or superiority over central scheduling.",
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    write_report(parser.parse_args().results)
