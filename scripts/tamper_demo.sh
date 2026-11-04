#!/usr/bin/env bash
# Demonstrate fail-closed verification on a COPY of a trial bundle (the original is never touched):
# 1. verify the copy as recorded -> analyzed, matches the saved verdict
# 2. change one word in the copy's oracle.jsonl -> INTEGRITY FAILURE, nothing analyzed, exit 1
# Usage: scripts/tamper_demo.sh runs/eval-v1/tr_0004
set -uo pipefail
cd "$(dirname "$0")/.."
src="$1"
tmp=$(mktemp -d)
cp -r "$src" "$tmp/"
copy="$tmp/$(basename "$src")"
rm -rf "$copy/reanalysis"
echo "== verify the untouched copy"
.venv/bin/python -m harness analyze "$copy" | tail -1
echo "== change one verdict word in the copy's oracle.jsonl (correct -> failed)"
sed -i '0,/"verdict": "correct"/s//"verdict": "failed"/' "$copy/oracle.jsonl"
.venv/bin/python -m harness analyze "$copy" | tail -1
echo "exit status $? (non-zero = refused)"
rm -rf "$tmp"
