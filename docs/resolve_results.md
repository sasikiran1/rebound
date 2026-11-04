# Resolve results (Nov 3, 2026)

Protocol: [resolve_protocol.md](resolve_protocol.md). Held-out cases frozen in `d63d0dd` before any model saw them;
prompts `resolve_interpret_v2.md` / `resolve_revise_v2.md` and decision code frozen at `46d4250` for every held-out
run below (later commits change display text and add an opt-in confirm mode that these runs do not use). Synthetic
fixtures, scripted customer turns, hand-written cases. 24 cases is a prototype check, not a reliability estimate.
Run records: `runs/resolve/` (not in git).

## Held-out outcomes (24 conversations, judged in SQL)

| Arm | Model | Correct | Unauthorized | Missed booking | Ideal question count | Model calls | Spend |
|---|---|---|---|---|---|---|---|
| **resolve** | Muse Spark 1.3 | **23** | **1** (H15) | 0 | 23 of 23 | 34 | $0.234 |
| one-shot | Muse Spark 1.3 | 15 | 2 (H15, H16) | 7 | (cannot ask) | 24 | $0.132 |
| form (correct entries by construction) | none | 24 | 0 | 0 | 22 of 23 | 0 | $0 |
| resolve | qwen3-14b, local | 16 | 2 (H17, H22) | 6 | 19 of 23 | 47 | $0 |
| one-shot | qwen3-14b, local | 10 | 0 | 14 | (cannot ask) | 24 | $0 |
| **rule parser** (no model, frozen in `ec32419`) | none | **20** | **2** (H10, H11) | 2 | 20 of 23 | 0 | $0 |
| resolve | Llama 3.1 8B, local | 10 | 2 (H08, H09) | 12 | 15 of 23 | 55 | $0 |
| **resolve + confirm** (product mode) | Muse Spark 1.3 | **23** | **0** | 1 (H15) | (one more per booking) | 35 | $0.246 |
| rule parser + confirm | none | 20 | 0 | 4 | (one more per booking) | 0 | $0 |

"Correct" counts both correct bookings and correct refusals to book (4 cases must end with no booking).
The gate set before the run was at least 22/24 and zero unauthorized bookings: **Muse resolve meets the first part (23/24) and
misses the second (1 unauthorized). With confirm mode, Muse meets both (23/24, 0 unauthorized).** Locally neither part
is met. Every row is a single run; the last four rows were added later the same day (below).

### By category (Muse)

| Category | resolve | one-shot |
|---|---|---|
| straightforward (4) | 4 | 4 |
| conditional alternatives (4) | 4 | 3 |
| negation (3) | 3 | 3 |
| references to history (4) | 3 | 0 (3 missed, 1 unauthorized) |
| corrections (3) | 3 | 1 (1 unauthorized, 1 missed) |
| renewed permission (3) | 3 | 1 |
| must stay unresolved (3) | 3 | 3 |

The gap between the two Muse arms (23 vs 15) sits where the design says it should: grounding in booking history,
asking before a trade-off, and applying a later correction. On plain, conditional and negated requests a single
call already does as well.

### The failures

- **H15, Muse resolve and one-shot (unauthorized).** "Keep the same time of day as my current booking, just move it
  to Friday." The model wrote "before 12:00" instead of the current start, 10:30, and 09:00 was booked. The quote
  check passed (the words were real); the meaning was coarsened. In confirm mode the customer would have been asked
  "Friday 9:00 AM, shall I book it?" before any write; this run did not use confirm mode.
- **H16, Muse one-shot (unauthorized).** The correction "Sorry, I meant Thursday morning" was not applied by the single
  call; the afternoon slot was booked.
- **H17, local (unauthorized).** "Friday" resolved to the 12th (a Thursday); the correction then dropped the date. A
  weekday-versus-date consistency check would catch this class; it was identified on held-out data, so it is *not*
  applied to these runs and is listed as a follow-up.
- **H22, local (unauthorized).** "The technician I had in September" matched two visits; the model picked one instead
  of marking it unclear. The deterministic ambiguity check only covers shared first names.
- Local misses concentrate on conditional alternatives (H05-H07), negation (H11), a negative reference (H14) and
  one permission case (H19).

## Two separate questions

1. **Did the final database effect satisfy the authorized request?** With Muse, in 23 of 24 cases; one booking (H15)
   was outside what the customer authorized. The form, given correct structured entries, satisfied all 24: the
   shared execution path (evaluator, store, worker) made no errors when the intent was right.
2. **Did conversational interpretation help?** Against one-shot extraction with the same model, yes: +8 cases, and
   the gain is in references, corrections and renewed permission. Against a capable form filled in correctly, no:
   the form is at least as correct. Effort is exploratory and measured on my own cases: the customer typed 401 words over
   the 24 conversations; the form needed 160 values set (kinds, dates, times, technicians, strengths, scopes).
   Whether 401 words of natural text are less work than 160 form values for real customers is not measured here.

## Does the model beat hand-written rules?

`resolve/eval/rules.py` is a deterministic interpreter: patterns for weekdays and ordinal dates, "this/next week",
parts of the day, "after/before/between" times, negation, "if not / otherwise" fallbacks, technician names (shared
first names become `unclear`) and history references ("same technician as last time", "whoever did my boiler
service in October"); a later message replaces the kinds of rules it mentions. It plugs into the same service,
validator, evaluator, worker and oracle. It was developed on the dev cases only (4/6, 5/6, 6/6 over three iterations,
`runs/resolve/rules-dev-r1..3`), frozen in commit `ec32419`, and run once on held-out. Its author had read the
held-out conversations, which can only favor it.

| Category | Muse resolve | rule parser |
|---|---|---|
| straightforward (4) | 4 | 4 |
| conditional alternatives (4) | 4 | 4 |
| negation (3) | 3 | 1 (2 unauthorized) |
| references to history (4) | 3 (1 unauthorized) | 4 |
| corrections (3) | 3 | 2 |
| renewed permission (3) | 3 | 2 |
| must stay unresolved (3) | 3 | 3 |
| **total** | **23, 1 unauthorized** | **20, 2 unauthorized** |

The model wins by 3 cases, not by a landslide, and the two fail differently:

- **Rules break on wording.** H10 "Anyone but Victor": the clause split on "but" lost the negation and Victor was
  booked. H11 "afternoons only": the plural did not match "afternoon" and 10:00 was booked. H16 "I meant Thursday
  morning. The afternoon is when I pick up my kids": two parts of the day contradict. H21 "Wednesday with Dana works
  too": resolved to this Wednesday, not the one after Tuesday the 17th.
- **The model breaks on meaning.** H15 "same time of day as my current booking" became "morning"; the rules, which
  read the current start time literally, got it right.

Honest reading: on these 24 cases a careful rule parser gets most of the way for free. The model's advantage is
robustness to phrasing (negation scope, plurals, relative dates, self-corrections), which matters more as real
customers write things no rule anticipated; this set is too small and too familiar to its author to measure that.

## Confirm mode on the held-out set

Product mode (`--confirm`): before any write the worker asks "I can move you to <slot>. Shall I book it?". The
simulated customer answers yes only if that slot is one the case file says they authorized, otherwise "No, that time
doesn't work for me." Every other question gets the usual scripted reply. Same frozen interpreters.

- Muse + confirm: 23/24 correct, **0 unauthorized**; H15 becomes a refused proposal (no booking) instead of a wrong
  booking. 35 calls, $0.246.
- Rule parser + confirm: 20/24, **0 unauthorized**; H10 and H11 become refusals.

So the customer's yes removes every unauthorized booking in both arms, at the cost of one more turn per booking. The
simulated customer checks every proposal against its own truth; real customers may say yes without reading, so this
measures the mechanism, not real-world vigilance.

## Llama 3.1 8B with the same validator

The study's Llama result (0 of 24 goal-preservation tasks through raw tool use) left open whether the model or the
setup failed. Inside Resolve, where the model only proposes a validated intent: 10/24, 2 unauthorized (H08, H09),
12 missed bookings, 55 calls, local. The first attempt crashed the run: Llama returned rules as strings, and ids were
assigned before the schema check (`runs/resolve/heldout-llama31/resolve.crashed-before-schema-fix.log`); malformed
output is now a validator problem with a repair turn. Llama 3.1 8B is weaker than Muse here, but not useless once code
holds it to a schema: most of its failures are refusals to book, not wrong bookings.

## Development record (dev cases, not held out)

| Run | Model / prompt | Correct | Unauthorized |
|---|---|---|---|
| dev-r1 | qwen3-14b, [v1](../prompts/resolve_interpret_v1.md) | 1/6 | 3 |
| dev-r2, dev-r3 | qwen3-14b, v2 (one corrective iteration, structural) | 4/6 | 0 |
| dev-r4 | qwen2.5-14b / gemma4-12b, v2 | 2/6 / 3/6 | 2 / 1 |
| muse-dev | Muse Spark 1.3, v2 | 6/6 | 0 |

## Recovery checks (live stack, `runs/resolve/faults/faults-r8`)

**17 of 17 pass** (targeted checks, one run each; they show the mechanism under the injected conditions, not a
reliability rate). Run 8 follows an outside review of the code on Nov 3 that found four defects; all four were real and
are fixed (decision record 0008):

- **A superseded uncertain write could still be sent.** After a timeout the request returned to `ready` with its v1
  attempt kept; a correction then approved v2, and the next worker resent v1's slot and key and completed without
  evaluating v2 (reproduced: Thursday booked under a Friday-only v2, `replayed=False`). Now the worker never resends
  an attempt from an older version: it voids that key through FieldDesk, which reports the change if it committed
  and otherwise makes the key unusable, so a copy still in flight cannot land. `superseded_attempt`: v1's key voided,
  v2's slot booked, a late v1 write refused with `key_voided`, one change. `superseded_committed`: the v1 write had
  committed before the correction; the worker learns that from the key without resending, then moves the
  appointment to what v2 allows (two changes, each authorized when made).
- **State and customer message were separate writes.** A crash between them could leave a question with no email.
  The state change, the assistant turn and its notification are now one transaction (`atomic_question`).
- **Immediate retries.** A released request was claimed again at once and Retry-After was discarded. Now it waits
  for the server's Retry-After, or 1 s doubling to 30 s with jitter (`backoff`).
- **Leases without fencing.** Leases were not renewed and every compose replica used the same owner name. Owners are
  now unique per process, every claim starts a new lease generation that guards the worker's writes, and the lease is
  renewed right before the booking POST; a worker that paused past its lease does not send (`stale_worker`).

Run 8 also scopes the notification check to its own request (other runs had left more due messages than one pass
sends). Earlier runs follow.

## Recovery checks, earlier run (`runs/resolve/faults/faults-r6`)

**12 of 12 pass.** Run 6 adds **calendar sync**: a real CalDAV server (Radicale) holds the technicians' calendars;
the original appointment is mirrored first and stale copies are planted in the other technicians' calendars; the
server is stopped, the worker books (the booking does not wait), the sync stays queued with its connection error,
and after restart one pass puts the event in the new technician's calendar at the booked time and removes the
copies; a second pass changes nothing. In that run the booking moved to another technician, so the removal path was
exercised. The other 11 checks passed unchanged. Runs 4 and 5 add two checks (run 4 failed the new notification check on a test bug: it counted
other checks' emails to the same fixture customer; it now matches messages by Message-ID). **Notifications:** the confirmation question and the outcome reach the
customer by SMTP; while the mail server refuses (421), the message stays queued with its error and is delivered on
the next attempt. **Fault shapes beyond latency:** the database link reset in the middle of the write (FieldDesk
answers 500; the worker keeps the attempt as uncertain, reconciles when the link is back, and the write commits
exactly once because the reset aborted the first transaction) and the link going silent during the search (timeout,
retry later, booked once). Earlier runs: an eligible slot at row 61 of 61 (first page 50) is found and booked; seven invalid-intent kinds are
rejected with no booking; a worker killed after claim, after recording its attempt, or after the POST returned leads
to exactly one change; a real lost acknowledgment (database link slowed during the write, worker timed out, commit
landed) reconciles with `replayed: true`; an approved slot taken before execution leads to a new question, not a
different booking; a correction does not drop the Friday rule; confirm mode books nothing before an explicit yes.
Run 1 found a validator crash on an invented technician id (fixed); run 2 was 8/8; run 3 added the confirm check.

## Demonstration (`runs/resolve/demo/demo-muse-1`, Muse, 2 model calls, $0.014)

Dev case D1: "after three this week, Dana if possible, check with me before next week." Real outage (5 s latency on
the database link): the worker's search times out and the request waits; meanwhile another customer takes Friday
15:30 with Dana. After recovery the worker finds no authorized slot and asks, offering two real open slots and quoting
the words they break. Scripted reply "Monday is okay, but not before four": version 2 widens the date range to Monday
and adds a 4 PM floor on Monday only; every other rule is unchanged. Confirm mode asks "Monday Nov 16 at 4:30 PM with
Alex, shall I book it?"; scripted yes (version 3). The worker process is killed right after its POST returns; the
commit is in SQL. A new worker reclaims the expired lease, resends with the same key, gets `replayed: true`. SQL: one
change, to the expected slot, authorized when applied.

## Spend

Muse for Resolve: 69 calls, **$0.443** (dev $0.063, held-out resolve $0.234, held-out one-shot $0.132, demo $0.014),
within the approved $2.55 / 141-call cap. Median call latency 4.4 s, 90th percentile 9.1 s. The deployment check
(`scripts/resolve_service_e2e.py`: intake over HTTP, worker container, confirm, SQL verdict `correct`) added 1 call,
$0.006: 70 calls, $0.449. The confirm-mode held-out run (approved cap $0.50 / 60 calls) used 35 calls, $0.246, and the
deployed check with notifications 1 call, $0.003: **106 calls, $0.698** for Resolve in all. Cumulative project spend
recorded: $50.77. The rule parser and Llama runs are free.

## Limitations

- Synthetic fixtures and scripted replies written by hand; I also wrote the expected outcomes and the form
  entries. No customer study; the effort comparison is exploratory.
- 24 held-out cases: one run per arm and model. No confidence intervals are claimed.
- The rule-parser comparison is one frozen run by an author who knew the cases; it shows the margin on this set,
  not how either would fare on wording neither has seen.
- Notifications go over SMTP to a local test inbox (`resolve/eval/mailsink.py`), not to a real provider or phone;
  recipients are synthetic (`customer-<id>@example.test`). Replies by email are not read back.
- The worker guarantee is the tested one (one worker per request, the injected crash points, FieldDesk's
  idempotency key), not general exactly-once execution.
- Interpretation errors that quote real words but change their meaning (H15) pass the validator; confirm mode stops
  them only if the customer reads the proposal, which the simulated customer always does.
