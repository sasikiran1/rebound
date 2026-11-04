#!/usr/bin/env bash
# Run trials back to back with the protocol's 60 s cool-down and an idle check between them.
# Usage: scripts/queue.sh <experiment> <manifest> "<condition> <gate> [extra args]" ...
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
exp="$1"; manifest="$2"; shift 2
mkdir -p "runs/$exp"
for spec in "$@"; do
  read -r cond gate extra <<<"$spec"
  echo "=== $(date -u +%FT%TZ) $cond gate=$gate $extra" | tee -a "runs/$exp/queue.log"
  # shellcheck disable=SC2086
  taskset -c 8-15 uv run python -m harness trial --manifest "$manifest" --condition "$cond" --gate "$gate" \
    --experiment "$exp" $extra >>"runs/$exp/queue.log" 2>&1
  echo "=== exit $? $(date -u +%FT%TZ)" | tee -a "runs/$exp/queue.log"
  sleep 60
  # CPU idle check: wait until the 5 s load of the container CPUs settles.
  for _ in $(seq 1 12); do
    busy=$(top -bn2 -d1 | awk '/^%Cpu/{v=$8} END{print 100-v}')
    awk -v b="$busy" 'BEGIN{exit !(b < 15)}' && break
    sleep 5
  done
done
echo "=== queue done $(date -u +%FT%TZ)" | tee -a "runs/$exp/queue.log"
