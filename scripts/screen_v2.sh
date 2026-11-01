#!/usr/bin/env bash
# Screening v2 (docs/screening_v2.md): one block of six trials, healthy reference first, then the seeded order.
# Usage: scripts/screen_v2.sh 1   (then, after checking block 1)   scripts/screen_v2.sh 2
# About 80 min and about $8.5 of model spend per block. Run the two smoke checks in the plan first.
set -euo pipefail
cd "$(dirname "$0")/.."
b="${1:?block number 1 or 2}"
m=manifests/exploratory/screen_v2.yaml
ref="agent on --no-fault --block $b --label b$b-healthy-reference"
agent="agent on --block $b --label b$b-agent"
guard="agent on --retry-after-guard --block $b --label b$b-agent-guard"
paced="scripted on --block $b --label b$b-scripted-paced"
unpaced="scripted on --unpaced --block $b --label b$b-scripted-unpaced"
bg="background on --block $b --label b$b-background-only"
case "$b" in
  1) scripts/queue.sh screen-v2 "$m" "$ref" "$guard" "$unpaced" "$paced" "$agent" "$bg" ;;
  2) scripts/queue.sh screen-v2 "$m" "$ref" "$guard" "$bg" "$paced" "$unpaced" "$agent" ;;
  *) echo "block must be 1 or 2" >&2; exit 2 ;;
esac
