#!/usr/bin/env bash
# The Resolve demo on Muse Spark (paid; at most 6 calls / $0.25). Usage: scripts/resolve_demo_muse.sh [run-name]
# Shows the technicians' calendars when the resolve-calendar container is up (compose profile `resolve`); stop the
# resolve-worker container first, or it takes the demo's request.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export RESOLVE_CALDAV_URL="${RESOLVE_CALDAV_URL:-http://127.0.0.1:5232/fielddesk/}"
export RESOLVE_CALDAV_USER="${RESOLVE_CALDAV_USER:-fielddesk}" RESOLVE_CALDAV_PASSWORD="${RESOLVE_CALDAV_PASSWORD:-local-only}"
uv run python -m resolve.eval.demo --model muse-spark-1.3 --run "${1:-demo-muse}" --max-calls 6 --max-cost 0.25
