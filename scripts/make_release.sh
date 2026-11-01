#!/usr/bin/env bash
# Package trial bundles for publication (runs/ is gitignored): one .tgz per trial, experiment-level files beside
# them, and a top-level SHA256SUMS over every archive. Bundles are packed as recorded; nothing inside is changed.
# Usage: scripts/make_release.sh <out_dir> <experiment>...
set -euo pipefail
cd "$(dirname "$0")/.."
out="$1"; shift
mkdir -p "$out"
for exp in "$@"; do
  mkdir -p "$out/$exp"
  for t in runs/"$exp"/tr_*; do
    [ -d "$t" ] || continue
    tar -czf "$out/$exp/$(basename "$t").tgz" -C "runs/$exp" "$(basename "$t")"
  done
  for f in runs/"$exp"/*; do  # experiment-level files: queue log, summaries, reports, campaign tables
    [ -f "$f" ] && cp "$f" "$out/$exp/"
  done
  for d in runs/"$exp"/*/; do  # non-trial folders (e.g. semantic runs) packed whole
    case "$(basename "$d")" in tr_*) continue ;; esac
    tar -czf "$out/$exp/$(basename "$d").tgz" -C "runs/$exp" "$(basename "$d")"
  done
done
(cd "$out" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
echo "release written to $out: $(find "$out" -type f | wc -l) files, $(du -sh "$out" | cut -f1)"
