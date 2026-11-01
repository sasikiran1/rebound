# Protocol v2 draft (not frozen, not yet used)

`docs/protocol.md` is the protocol eval-v1 was frozen under (commit `81c912f`) and stays unchanged. This file collects
the changes from the `audit-fixes` branch (merged 2026-10-28, after eval-v1 ran) and from the local-model study, for
the next frozen configuration. None of them was in force for eval-v1.

From the 2026-10-27 audit (`audit-fixes`):

- Completion time uses FieldDesk's commit acknowledgment (`completion_basis`); bundles without it use
  `appointment_changes.applied_at`, the INSERT time one round trip before COMMIT, and say so.
- Every acceptance criterion ends in pass, fail or insufficient_evidence; missing inputs never pass.
- Evidence from more than one compatibility fingerprint or analyzer is not judged (`analysis.experiment.summarize`
  can judge one named fingerprint and lists every other valid trial as excluded).
- Re-analysis fails closed on raw checksum mismatches and writes only to `<trial>/reanalysis/`.
- The per-trial recovery contract (`analysis/contracts.py`) and back-pressure compliance are reported.
- Declared arms available: scripted baseline `robust_v2`, background-only, runtime Retry-After guard; agent
  continuation `stateless_reasoning`. The screening design is predeclared in `docs/screening_v2.md`.

From the local-model study and eval-v1 (2026-10-28):

- Tool set `prompts/tools_v2_time.json` (optional start-time filter): the fix that gave 8 of 8 correct afternoon
  bookings (`manifests/exploratory/eval_v2_draft.yaml`).
- Oracle task `constraint` (start hour, technician, exact slot) and deferral judged against what is still free.
- Runtime-dropped output is labeled (`limit = runtime_dropped_output`), not counted as a model give-up.
- Any spend cap needed to hold a budget is set in the frozen manifest itself, so every trial shares one fingerprint
  (eval-v1's tr_0018 shows the cost of not doing so).

The freeze will restate every rule in one place and record its commit in a new freeze file before any trial.
