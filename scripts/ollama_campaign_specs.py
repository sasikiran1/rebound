"""Write the recovery-campaign manifests and trial lists from docs/ollama_protocol.md (no hand-typed values).

Usage: python scripts/ollama_campaign_specs.py
Reads the ctx16k semantic results for both campaign models, applies the declared arrival-rate rule
(0.35 / (C x L) of the slower model, rounded down to 0.01; C = median model calls per goal, L = median model-call
latency excluding each goal's first call, which includes model loading), then writes:
  manifests/ollama/recovery_<tag>_<set>.yaml   per model and task set (A = pilot, B = blocks)
  manifests/ollama/pilot_v1.specs               Llama B and D on set A
  manifests/ollama/blocks_v1.specs              three blocks of 10, each shuffled with seed 20261028 + block
  manifests/ollama/rate_rule.json               the inputs and the result
"""

import json
import math
import random
import statistics as st
from pathlib import Path

import yaml

# llama3.1:8b failed the protocol's entry check (0/12 correct, sem_llama31_v1), so the campaign is gemma4 only.
MODELS = {"gemma4": ("gemma4-12b-16k:latest", Path("runs/ollama-semantic/sem_v1/results.json"), False)}
TASKS = {"A": ("data/tasks/tasks_dev_A.jsonl", "1cf371381b1f8c8271a173c3a2171a3fe8b476eea34eab13c12688c1479710e0"),
         "B": ("data/tasks/tasks_eval_B.jsonl", "353954f9da39099571ce903813bc770e0b4e687a3118b1c9aa77c511a4fab341")}
OUT = Path("manifests/ollama")


def rate_inputs() -> dict:
    res = {}
    for tag, (mid, path, _) in MODELS.items():
        rows = [x for x in json.loads(path.read_text()) if x.get("model") == mid and x.get("setting") == "ctx16k"
                and not x["case"].startswith("RACE") and x["scenario"]["class"] != "model_runner_failure"]
        lat = [v for x in rows for v in x["model_latency_s"][1:]]
        res[tag] = {"goals": len(rows), "C": st.median(x["model_calls"] for x in rows), "L_s": st.median(lat)}
    slow = max(res.values(), key=lambda r: r["C"] * r["L_s"])
    rate = math.floor(100 * 0.35 / (slow["C"] * slow["L_s"])) / 100
    return {"per_model": res, "rule": "floor_0.01(0.35 / (C * L)) of the slower model", "goals_per_s": rate}


def manifest(tag: str, task_set: str, rate: float) -> dict:
    base = yaml.safe_load(Path("manifests/exploratory/explore_v1.yaml").read_text())
    mid, _, think = MODELS[tag]
    base["experiment"] = "ollama-recovery-v1"
    base["tasks"] = {"file": TASKS[task_set][0], "sha256": TASKS[task_set][1]}
    base["arrivals"] = {"process": "poisson", "goals_per_s": rate, "seed": 7, "start_s": 0, "stop_s": 370}
    base["windows"] = {"warmup": [0, 60], "baseline": [60, 180], "observe_s": 180, "drain_deadline_s": 240}
    base["gate"] = {"tokens": 12, "queue_max": 24, "queue_wait_ms": 1500, "retry_after_s": 2}
    base["model"] = {"id": mid, "api": "responses", "continuation": "full_history", "reasoning_effort": "low",
                     "max_output_tokens": 2048, "parallel_tool_calls": False, "prompt_cache_key": "rebound-agent-v1",
                     "prompt": "prompts/agent_v1.md", "tools": "prompts/tools_v1.json", "provider": "ollama",
                     "ollama_options": {"temperature": 0, "seed": 41, "num_ctx": 16384}, "think": think}
    return base


def main() -> None:
    r = rate_inputs()
    if r["goals_per_s"] <= 0:
        raise SystemExit(f"rate rule gives {r}; stop and report")
    (OUT / "rate_rule.json").write_text(json.dumps(r, indent=2) + "\n")
    paths = {}
    for tag in MODELS:
        for s in TASKS:
            p = OUT / f"recovery_{tag}_{s}.yaml"
            p.write_text(yaml.safe_dump(manifest(tag, s, r["goals_per_s"]), sort_keys=False))
            paths[tag, s] = p
    pilot = [f"{paths['gemma4', 'A']} agent off 0 pilot-gemma4-B", f"{paths['gemma4', 'A']} agent on 0 pilot-gemma4-D"]
    (OUT / "pilot_v1.specs").write_text("\n".join(pilot) + "\n")
    lines = []
    for block in (1, 2, 3):
        arms = []
        for tag in MODELS:
            p = paths[tag, "B"]
            arms += [f"{p} agent off {block} {tag}-A --no-fault", f"{p} agent off {block} {tag}-B",
                     f"{p} agent on {block} {tag}-C --no-fault", f"{p} agent on {block} {tag}-D"]
        p = paths["gemma4", "B"]
        arms += [f"{p} background off {block} bg-B", f"{p} background on {block} bg-D"]
        random.Random(20261028 + block).shuffle(arms)
        lines += [f"# block {block} (shuffle seed {20261028 + block})"] + arms
    (OUT / "blocks_v1.specs").write_text("\n".join(lines) + "\n")
    print(json.dumps(r, indent=2))
    print((OUT / "blocks_v1.specs").read_text())


if __name__ == "__main__":
    main()
