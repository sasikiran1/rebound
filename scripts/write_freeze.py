"""Write docs/OLLAMA_FREEZE.md: the frozen commit and SHA-256 of the files that define the campaign."""

import hashlib
import subprocess
from pathlib import Path

FROZEN_COMMIT = "3af7c23bc0ff845c7f6ab486a14540dd5ee4a9f3"
FILES = ["docs/ollama_protocol.md", "manifests/ollama/blocks_v1.specs", "manifests/ollama/recovery_gemma4_B.yaml",
         "manifests/ollama/rate_rule.json", "prompts/agent_v1.md", "prompts/tools_v1.json", "harness/ollama.py",
         "harness/agent.py", "harness/trial.py", "harness/adapter.py", "backend/fielddesk/app.py",
         "backend/fielddesk/gate.py", "analysis/metrics.py", "analysis/recovery.py", "analysis/ollama_campaign.py"]

lines = ["# Freeze record: ollama-recovery-v1", "",
         f"Recorded before the first block trial. Configuration under test: the tree at commit {FROZEN_COMMIT}",
         "(clean). Each file below must hash the same at the start of every block trial "
         "(scripts/write_freeze.py --check).", "", "| File | SHA-256 | Same as frozen commit |", "|---|---|---|"]
for f in FILES:
    cur = hashlib.sha256(Path(f).read_bytes()).hexdigest()
    frozen = hashlib.sha256(subprocess.run(["git", "show", f"{FROZEN_COMMIT}:{f}"], capture_output=True,
                                           check=True).stdout).hexdigest()
    lines.append(f"| {f} | {cur} | {'yes' if cur == frozen else 'NO'} |")
lines += ["", "Pilot (exploratory, task set A, not part of the comparison): runs/ollama-pilot-v1 tr_0001 (B) and "
          "tr_0002 (D), both valid.",
          "Model: gemma4-12b-16k:latest, digest 61d4626bfb6cb772f62a94d562088ea34a427d32f86b20f15c13441823f19b54.",
          "Block order: manifests/ollama/blocks_v1.specs (seeded shuffles 20261029-20261031)."]
Path("docs/OLLAMA_FREEZE.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
