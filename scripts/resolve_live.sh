#!/usr/bin/env bash
# Live console: you type as the customer; a naive one-shot agent and Resolve act on the same seeded world.
# Usage: scripts/resolve_live.sh [--case H15] [--split heldout] [--model muse-spark-1.3] [--max-calls 40] [--max-cost 0.50]
# Default model is the local one (free). Stops the resolve-worker container first, or it would take the requests.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
docker compose -f docker/compose.yml --profile resolve stop resolve-worker > /dev/null 2>&1 || true
export RESOLVE_CALDAV_URL="${RESOLVE_CALDAV_URL:-http://127.0.0.1:5232/fielddesk/}"
export RESOLVE_CALDAV_USER="${RESOLVE_CALDAV_USER:-fielddesk}" RESOLVE_CALDAV_PASSWORD="${RESOLVE_CALDAV_PASSWORD:-local-only}"
exec uv run python -m resolve.eval.live "$@"
