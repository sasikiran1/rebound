#!/usr/bin/env bash
# Reproduce published results from a fresh clone, with no model key and no containers:
# clone -> uv sync --frozen -> restore one experiment from the release archive -> verify (fail closed, strict).
# Usage: scripts/clean_clone_check.sh <release_dir> <experiment> [branch]
set -euo pipefail
release="$1"; exp="$2"; branch="${3:-main}"
src="$(cd "$(dirname "$0")/.." && pwd)"
# Restored bundles take about 5 GB per experiment: put TMPDIR on a large disk. The folder is removed on exit.
work=$(mktemp -d "${TMPDIR:-/tmp}/rebound-clean.XXXX")
trap 'rm -rf "$work"' EXIT
git clone -q --branch "$branch" "$src" "$work/rebound"
cd "$work/rebound"
echo "clone at $(git log --oneline -1)"
[ ! -f .env ] && echo "no .env in the clone"
(cd "$release" && sha256sum -c --quiet SHA256SUMS) && echo "release archive sums OK"
env -u MODEL_API_KEY uv sync -q --frozen
mkdir -p "runs/$exp"
for f in "$release/$exp"/tr_*.tgz; do
  tar -xzf "$f" -C "runs/$exp"
done
echo "restored $(ls -d runs/"$exp"/tr_* | wc -l) trial bundles"
set +e
env -u MODEL_API_KEY uv run python -m harness analyze --strict "runs/$exp" > "$work/verify.log" 2>&1
code=$?
set -e
echo "verify exit status $code"
echo "matches saved verdict: $(grep -c 'MATCHES SAVED VERDICT' "$work/verify.log") of $(ls -d runs/"$exp"/tr_* | wc -l)"
grep -v 'MATCHES SAVED VERDICT' "$work/verify.log" | head -5 || true
echo "work dir $work (removed on exit)"
