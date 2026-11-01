# Freeze record: eval-v1

Recorded before the first evaluation trial, on 2026-10-28.

- Freeze commit: `81c912f99faeb6cc5ec69f8ecfd3b737bf716ddd` (branch `ollama-verify`, tree clean).
- Frozen manifest: `manifests/frozen/eval_v1.yaml`. Protocol: `docs/protocol.md`. Run list:
  `manifests/frozen/eval_v1.specs` (block shuffle seeds 20261102, 20261103, 20261104). Runner:
  `scripts/eval_campaign.sh manifests/frozen/eval_v1.specs 34.06 2.60`.
- Model: `muse-spark-1.3` (Meta Model API), full_history continuation, reasoning effort low.
- Spend before the evaluation: $14.04 recorded in trial bundles (about $14.06 with untracked single-goal smoke calls).
  Cap for the evaluation: $20 more, cumulative $34.06 across all trial bundles.
- Anything changed after this commit is a new configuration and needs new trials.

## Note added 2026-10-31

The history was rewritten on 2026-10-31 (see docs/commit_map.txt for where each recorded hash now points). Two
early planning-notes files were removed from every commit and comment lines that cited them were updated; no other
file content changed.
