#!/usr/bin/env bash
# Retry-After guard trials (docs/agent_v2_protocol.md, part B), with the same cumulative spend check as eval_campaign.sh.
# Usage: scripts/guard_campaign.sh <total_cap_usd> <agent_trial_estimate_usd>
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
cap="$1"; est="$2"; exp=agent-v2-guard; manifest=manifests/frozen/eval_v1.yaml
mkdir -p "runs/$exp"
log="runs/$exp/queue.log"
spent() { .venv/bin/python -c "import glob,json;print(round(sum(json.load(open(f)).get('spent_usd') or 0 for f in glob.glob('runs/*/tr_*/trial.json')),4))"; }
echo "=== guard start $(date -u +%FT%TZ) git=$(git rev-parse HEAD) cap=\$$cap est=\$$est" | tee -a "$log"
while read -r block label extra; do
  s=$(spent)
  if ! awk -v s="$s" -v e="$est" -v c="$cap" 'BEGIN{exit !(s + e <= c)}'; then
    echo "=== SKIPPED (budget) $label spent=\$$s" | tee -a "$log"
    continue
  fi
  echo "=== $(date -u +%FT%TZ) block=$block label=$label estimated \$$est, spent \$$s of \$$cap" | tee -a "$log"
  # shellcheck disable=SC2086
  taskset -c 8-15 .venv/bin/python -m harness trial --manifest "$manifest" --condition agent --gate on \
    --experiment "$exp" --block "$block" --label "$label" --retry-after-guard $extra >>"$log" 2>&1 </dev/null
  echo "=== exit $? $(date -u +%FT%TZ) spent_total=\$$(spent)" | tee -a "$log"
  sleep 60
done <<'SPECS'
1 guard-10s-1
2 guard-10s-2
3 guard-10s-3
4 guard-20s --fault-duration-s 20
SPECS
echo "=== guard done $(date -u +%FT%TZ) spent_total=\$$(spent)" | tee -a "$log"
