"""Write the frozen evaluation manifest and the seeded evaluation plan.

Usage: python scripts/freeze.py --tokens N --queue-wait-ms M

The frozen manifest is the exploration manifest with: task set B, the chosen gate parameters, the evaluation
experiment name, and the replay trace path of the healthy reference trial (always the first evaluation trial).
"""

import argparse
import json
import random
from pathlib import Path

import yaml

ORDER_SEED = 20261101
CELLS = [("agent", "off"), ("agent", "on"), ("scripted", "off"), ("scripted", "on"), ("replay", "off")]
EXPERIMENT = "eval-v1"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tokens", type=int, required=True)
    p.add_argument("--queue-wait-ms", type=int, required=True)
    p.add_argument("--unfamiliar-per-gate", type=int, default=2)
    args = p.parse_args()

    m = yaml.safe_load(Path("manifests/exploratory/explore_v1.yaml").read_text())
    template = json.loads(Path("data/build/template.json").read_text())
    m["experiment"] = EXPERIMENT
    m["tasks"] = {"file": template["task_sets"]["B"]["file"], "sha256": template["task_sets"]["B"]["sha256"]}
    m["gate"] = {"tokens": args.tokens, "queue_max": 2 * args.tokens, "queue_wait_ms": args.queue_wait_ms,
                 "retry_after_s": 2}
    m["replay_trace"] = f"runs/{EXPERIMENT}/tr_0001/client.jsonl"
    out = Path("manifests/frozen/eval_v1.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# FROZEN evaluation manifest. Do not edit: any change is a new configuration.\n"
                   + yaml.safe_dump(m, sort_keys=False, default_flow_style=None, width=120))

    rng = random.Random(ORDER_SEED)
    plan = [{"n": 1, "condition": "agent", "gate": "off", "args": "--no-fault --label healthy-reference",
             "note": "healthy reference trial; its client.jsonl is the replay trace"}]
    for block in (1, 2, 3):
        order = CELLS[:]
        rng.shuffle(order)
        for cond, gate in order:
            plan.append({"n": len(plan) + 1, "condition": cond, "gate": gate,
                         "args": f"--block {block} --repeat {block} --label block{block}-{cond}-gate-{gate}"})
    for i in range(1, args.unfamiliar_per_gate + 1):
        for gate in ("off", "on"):
            plan.append({"n": len(plan) + 1, "condition": "agent", "gate": gate,
                         "args": f"--fault-duration-s 20 --repeat {i} --label unfamiliar20s-agent-gate-{gate}-{i}"})
    Path("docs/eval_plan.json").write_text(json.dumps({"order_seed": ORDER_SEED, "experiment": EXPERIMENT,
                                                       "manifest": str(out), "trials": plan}, indent=2) + "\n")
    for t in plan:
        print(t["n"], t["condition"], t["gate"], t["args"])


if __name__ == "__main__":
    main()
