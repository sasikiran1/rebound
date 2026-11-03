#!/usr/bin/env bash
# Frozen held-out evaluation on the local model (no paid calls): resolve, one-shot and form arms.
# Usage: scripts/resolve_eval_local.sh <run-name> [model]
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=${1:?run name}
MODEL=${2:-qwen3-14b-16k:latest}
OUT=runs/resolve/$RUN
mkdir -p "$OUT"
git rev-parse HEAD > "$OUT/commit.txt"
git status --short > "$OUT/worktree.txt"
sha256sum data/resolve/heldout.yaml prompts/resolve_interpret_v2.md prompts/resolve_revise_v2.md > "$OUT/inputs.sha256"
for ARM in resolve oneshot form; do
  uv run python -m resolve.eval.run --split heldout --arm "$ARM" --model "$MODEL" --out "$OUT" 2>&1 | tee "$OUT/$ARM.log"
done
