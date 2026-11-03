#!/usr/bin/env bash
# Paid Muse run of the frozen Resolve code, one part at a time, each with its own hard cap.
# Usage: scripts/resolve_muse.sh <run-name> <part>   part: dev | resolve | oneshot
# Reads MODEL_API_KEY from .env (gitignored); never prints it.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
RUN=${1:?run name}
PART=${2:?part}
MODEL=muse-spark-1.3
OUT=runs/resolve/$RUN
mkdir -p "$OUT"
git rev-parse HEAD > "$OUT/commit.txt"
sha256sum data/resolve/heldout.yaml data/resolve/dev.yaml prompts/resolve_interpret_v2.md \
  prompts/resolve_revise_v2.md > "$OUT/inputs.sha256"
case "$PART" in
  dev)     uv run python -m resolve.eval.run --split dev --arm resolve --model $MODEL --out "$OUT" \
             --max-calls 25 --max-cost 0.50 2>&1 | tee "$OUT/dev.log" ;;
  resolve) uv run python -m resolve.eval.run --split heldout --arm resolve --model $MODEL --out "$OUT" \
             --max-calls 80 --max-cost 1.30 2>&1 | tee "$OUT/resolve.log" ;;
  oneshot) uv run python -m resolve.eval.run --split heldout --arm oneshot --model $MODEL --out "$OUT" \
             --max-calls 30 --max-cost 0.50 2>&1 | tee "$OUT/oneshot.log" ;;
  *) echo "unknown part $PART"; exit 2 ;;
esac
