#!/usr/bin/env bash
# Live flow screen: the live console behind a browser page (resolve/eval/live_web.py).
# Usage: scripts/resolve_live_ui.sh [--model muse-spark-1.3] [--max-calls 40] [--max-cost 0.50] [--case D1] [--port 8092]
# then open http://127.0.0.1:8092/. Default model is the local one (free).
# Stops the resolve-worker container first, or it would take the requests.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
port=8092
while [ $# -gt 0 ]; do
  case "$1" in
    --model) export RESOLVE_LIVE_MODEL="$2"; shift 2 ;;
    --max-calls) export RESOLVE_LIVE_MAX_CALLS="$2"; shift 2 ;;
    --max-cost) export RESOLVE_LIVE_MAX_COST="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --case) export RESOLVE_LIVE_CASE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
docker compose -f docker/compose.yml --profile resolve stop resolve-worker > /dev/null 2>&1 || true
export RESOLVE_CALDAV_URL="${RESOLVE_CALDAV_URL:-http://127.0.0.1:5232/fielddesk/}"
export RESOLVE_CALDAV_USER="${RESOLVE_CALDAV_USER:-fielddesk}" RESOLVE_CALDAV_PASSWORD="${RESOLVE_CALDAV_PASSWORD:-local-only}"
exec uv run uvicorn resolve.eval.live_web:app --host 127.0.0.1 --port "$port" --log-level warning
