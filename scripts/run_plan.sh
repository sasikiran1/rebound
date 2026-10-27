#!/usr/bin/env bash
# Run docs/eval_plan.json trials FROM..TO (1-based, inclusive) through scripts/queue.sh.
# Usage: scripts/run_plan.sh FROM TO
set -euo pipefail
cd "$(dirname "$0")/.."
from="$1"; to="$2"
specs=()
while IFS= read -r line; do specs+=("$line"); done < <(
  python3 -c "
import json, sys
p = json.load(open('docs/eval_plan.json'))
for t in p['trials']:
    if $from <= t['n'] <= $to:
        print(t['condition'], t['gate'], t['args'])
")
manifest=$(python3 -c "import json; print(json.load(open('docs/eval_plan.json'))['manifest'])")
exp=$(python3 -c "import json; print(json.load(open('docs/eval_plan.json'))['experiment'])")
scripts/queue.sh "$exp" "$manifest" "${specs[@]}"
