"""Write manifests/frozen/eval_v1.specs: the frozen evaluation run list (docs/protocol.md).

Order: the healthy reference trial (agent, gate off, no fault; its client.jsonl is the replay trace), then three
blocks of the five headline cells, each shuffled with seed 20261101 + block, then the unfamiliar 20 s fault
(agent gate off, agent gate on: the plan's minimum of one each).
Line format: <condition> <gate> <block> <label> [extra harness args]
"""

import random
from pathlib import Path

CELLS = [("agent", "off"), ("agent", "on"), ("scripted", "off"), ("scripted", "on"), ("replay", "off")]
lines = ["# frozen evaluation run list (scripts/eval_specs.py)", "agent off 0 reference --no-fault"]
for block in (1, 2, 3):
    cells = list(CELLS)
    random.Random(20261101 + block).shuffle(cells)
    lines.append(f"# block {block} (shuffle seed {20261101 + block})")
    lines += [f"{c} {g} {block} {c}-gate-{g}" for c, g in cells]
lines += ["# unfamiliar fault, 20 s", "agent off 4 unfamiliar-agent-gate-off --fault-duration-s 20",
          "agent on 4 unfamiliar-agent-gate-on --fault-duration-s 20"]
Path("manifests/frozen/eval_v1.specs").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
