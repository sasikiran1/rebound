# Limitations

What Rebound's results do and do not support, as of 2026-10-28. Numbers are in `docs/results.md`.

## Scope

- One API (FieldDesk rescheduling), one dependency fault (400 ± 50 ms latency on the API→PostgreSQL link, 10 s and
  one 20 s trial pair), one admission-gate setting (12 tokens, queue 24, 1.5 s wait, Retry-After 2 s), one laptop.
- One paid model (Muse Spark 1.3) in the frozen evaluation. Local-model runs (gemma4, qwen variants, Llama, Mistral)
  are a separate, labeled study with different pacing; they are not pooled with eval-v1.
- Synthetic data only (seeded generator, 40,000 customers). Task sets A and B never overlap in region, service and date,
  so goals cannot steal each other's slots except in the deliberately constructed race cases.

## Measurement

- **T_rec has a floor of about 10 s.** Health uses requests arriving in the trailing 10 s window, which still holds
  fault-period requests until 10 s after the toxic is removed. "T_rec = 10 s" means recovered at once; lost
  background service D is reported beside it for that reason.
- T_rec says when sustained health (60 consecutive seconds) returned; it does not say how much service was lost.
- DB round-trip milliseconds include the injected proxy latency and are not database work. Postgres CPU-seconds
  (cgroup) are the work measure (local-model campaign only; eval-v1 predates the sampler).
- Completion time is the change row's insert time (one round trip before COMMIT), not the commit itself.

## Load and attribution

- **Agents are a small share of the load.** Goal clients held 0.4–1.9% of in-flight request time during the fault in
  eval-v1; background traffic (520 req/s, declared) held the rest. Recovery and lost-service effects are therefore
  driven by background traffic and the gate, not by agents. Background-only arms (local-model campaign) show the same
  gate effect without any agent.
- With the original stored-response pacing, agents ran about 10 s per step and complied with Retry-After only because
  each model call outlasted it. With full_history pacing (about 0.8 s per step) they ignored it every time.
- Not tested: the original target of 8–16 concurrent agents (about 1.7 were active at once). Inference only: with
  520 req/s of background traffic their share would likely stay in single-digit percent.

## Statistics

- Three paired blocks per headline cell; the 20 s fault has one pair. Effects are reported per block, with no
  population-level rate or confidence interval.
- Agent runs use temperature defaults of the provider (Muse) or temperature 0 (local models). Deterministic local runs
  repeated under the same seed are replays, not independent samples.

## Deviations, all disclosed

- Observation window 300 s and arrivals to 490 s to keep evaluation spend under $20.
- Scripted think time 0.77 s (agents' median healthy step under full_history), not the exploration value of 10.25 s.
- tr_0018 ran from a copy of the frozen manifest differing only in `trial_budget_usd` (2.52) to hold the $20 cap;
  the strict analyzer therefore treats the 20 s pair as two configurations (C5 insufficient evidence).
- Retry-After guard: run in the separate agent-v2 study, on newer code than eval-v1, so its comparisons cross
  configurations.
- The original date-only oracle could not see customer time or technician constraints or a competitor taking the
  only slot. It now accepts an optional task `constraint` and judges a deferral against what is still free; earlier
  verdicts were produced without it, and finished trials cannot be re-scored because their databases are discarded.

## Not established

- That agents delay an API's recovery. Not observed at this load.
- That a Retry-After guard prevents lost goals in general. In agent-v2 it kept every goal (209 of 209 in three 10 s
  trials and one 20 s trial), but the 20 s result is one pair, and it missed its pre-registered bar for refusals.
- That requirement enforcement catches a misread customer. Code enforces what the model records, not what the
  customer meant; a wrong extraction goes through.
- That the bug hunter covers much ground. Its 24 scenarios were all time-of-day requests, and it found no failure.
- Anything about other APIs, faults, gate settings, models or hardware.
