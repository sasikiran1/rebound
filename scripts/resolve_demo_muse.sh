#!/usr/bin/env bash
# The Resolve demo on Muse Spark (paid; at most 6 calls / $0.25). Usage: scripts/resolve_demo_muse.sh [run-name]
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
uv run python -m resolve.eval.demo --model muse-spark-1.3 --run "${1:-demo-muse}" --max-calls 6 --max-cost 0.25
