#!/usr/bin/env bash
# Run the frozen evaluation list with a hard cumulative spend cap (60 s cool-down + CPU idle check, as queue.sh).
# Usage: scripts/eval_campaign.sh <specs_file> <total_cap_usd> <agent_trial_estimate_usd>
# Before every agent trial the recorded spend of ALL trial bundles is re-summed; if it plus the estimate would
# exceed the cap, the trial is skipped and logged as SKIPPED (budget), never run. The key is read from .env and never
# printed.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
specs="$1"; cap="$2"; est="$3"; exp=eval-v1; manifest=manifests/frozen/eval_v1.yaml
mkdir -p "runs/$exp"
log="runs/$exp/queue.log"
spent() { .venv/bin/python -c "import glob,json;print(round(sum(json.load(open(f)).get('spent_usd') or 0 for f in glob.glob('runs/*/tr_*/trial.json')),4))"; }
echo "=== eval start $(date -u +%FT%TZ) git=$(git rev-parse HEAD) cap=\$$cap est_per_agent_trial=\$$est spent_so_far=\$$(spent)" | tee -a "$log"
while read -r cond gate block label extra; do
  [ -z "${cond:-}" ] && continue
  case "$cond" in \#*) continue ;; esac
  if [ "$cond" = agent ]; then
    s=$(spent)
    if ! awk -v s="$s" -v e="$est" -v c="$cap" 'BEGIN{exit !(s + e <= c)}'; then
      echo "=== SKIPPED (budget) $cond gate=$gate block=$block label=$label spent=\$$s est=\$$est cap=\$$cap" | tee -a "$log"
      continue
    fi
    echo "=== estimated model cost for this trial: about \$$est (spent so far \$$s of \$$cap)" | tee -a "$log"
  fi
  echo "=== $(date -u +%FT%TZ) $cond gate=$gate block=$block label=$label $extra" | tee -a "$log"
  # shellcheck disable=SC2086
  taskset -c 8-15 .venv/bin/python -m harness trial --manifest "$manifest" --condition "$cond" --gate "$gate" \
    --experiment "$exp" --block "$block" --label "$label" $extra >>"$log" 2>&1 </dev/null
  echo "=== exit $? $(date -u +%FT%TZ) spent_total=\$$(spent)" | tee -a "$log"
  sleep 60
  for _ in $(seq 1 12); do
    busy=$(top -bn2 -d1 | awk '/^%Cpu/{v=$8} END{print 100-v}')
    awk -v b="$busy" 'BEGIN{exit !(b < 15)}' && break
    sleep 5
  done
done < "$specs"
echo "=== eval done $(date -u +%FT%TZ) spent_total=\$$(spent)" | tee -a "$log"
