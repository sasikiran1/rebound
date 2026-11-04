#!/usr/bin/env bash
# Run a list of local-model trials back to back (30 s cool-down + CPU idle check between them; docs/ollama_protocol.md).
# Usage: scripts/local_models/ollama_campaign.sh <experiment> <specs_file>
# Each line of specs_file: <manifest> <condition> <gate> <block> <label> [--no-fault]
# The Meta key is removed from the environment, so no trial can reach a paid model API; .env is never sourced.
set -uo pipefail
cd "$(dirname "$0")/../.."
unset MODEL_API_KEY
exp="$1"; specs="$2"
mkdir -p "runs/$exp"
log="runs/$exp/queue.log"
echo "=== campaign start $(date -u +%FT%TZ) specs=$specs sha256=$(sha256sum "$specs" | cut -c1-64) git=$(git rev-parse HEAD)" | tee -a "$log"
while read -r manifest cond gate block label extra; do
  [ -z "${manifest:-}" ] && continue
  case "$manifest" in \#*) continue ;; esac
  echo "=== $(date -u +%FT%TZ) $manifest $cond gate=$gate block=$block label=$label $extra MODEL_API_KEY_set=${MODEL_API_KEY+yes}" | tee -a "$log"
  # shellcheck disable=SC2086
  taskset -c 8-15 .venv/bin/python -m harness trial --manifest "$manifest" --condition "$cond" --gate "$gate" \
    --experiment "$exp" --block "$block" --label "$label" $extra >>"$log" 2>&1 </dev/null
  echo "=== exit $? $(date -u +%FT%TZ)" | tee -a "$log"
  sleep 30
  for _ in $(seq 1 12); do
    busy=$(top -bn2 -d1 | awk '/^%Cpu/{v=$8} END{print 100-v}')
    awk -v b="$busy" 'BEGIN{exit !(b < 15)}' && break
    sleep 5
  done
done < "$specs"
echo "=== campaign done $(date -u +%FT%TZ)" | tee -a "$log"
