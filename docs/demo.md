# Three-minute demo script

Recorded on the live screen (`http://127.0.0.1:8092/`, Story view, scenario 2), Meta Muse Spark 1.3. Setup, Story
view and fallbacks: [recording_runbook.md](recording_runbook.md). Start the screen with:

```bash
scripts/resolve_live_ui.sh --model muse-spark-1.3 --max-calls 20 --max-cost 0.45 --case D1
```

Scenario 2 costs 2 model calls per take (about $0.02). Rehearsal on Muse, Nov 3, through Play in a browser: the
outage, Fri 15:30 Dana taken, the question ("Nothing open meets everything you asked for. The closest options are
Wednesday Nov 11 at 11:00 AM with Dana or Friday Nov 13 at 10:00 AM with Alex…"), the reply, the proposal Mon 16:30
with Alex, yes, the worker killed after its write, reconciled with `replayed=True`, CORRECT, one change, Alex's
calendar; 2 calls, $0.018.

The caption in Story view reads "Synthetic customer · real model, API and faults · Meta Muse Spark 1.3 · recorded
run". Narrate what appears; if the model proposes a different slot, say what is on screen.

**Voiceover (about 407 words, 3:00 at 135 words a minute)**

**[0:00–0:35] The problem.** An AI agent told a customer, "Done." The API returned 200 OK. The customer had written
"6 pm only"; Meta's Muse Spark had booked ten in the morning. Only an independent SQL check noticed.

I'm Sasi, and this is Rebound. First, I measured real Muse Spark agents rescheduling appointments while I slowed the
API's database. With no way to wait, they retried before Retry-After every single time. Given a wait tool, never. The
failure was the interface, not the model.

**[0:35–0:55] The architecture.** Then I built Resolve on one rule: the model proposes, deterministic code decides,
SQL judges. The model never sees availability, so it cannot invent a slot. It turns the customer's words into typed
constraints, each quoting her exactly. A leased worker does the search, the waiting and the single idempotent write.

**[0:55–2:10] Live run.** Live run, synthetic customer. Maya writes: after three this week, Dana if possible, and
don't push into next week without checking with me. Muse turns that into rules, each with its quote.

Now I slow the booking database by five seconds. The worker's calls time out, so it waits and backs off. Nothing is
written.

While it waits, another customer takes Friday at three thirty with Dana. The database recovers. Nothing Maya allowed
is left, so Resolve asks instead of guessing.

She answers: Monday is okay, but not before four. Resolve records that as version two. Resolve shows the exact slot,
Monday four thirty with Alex, and books nothing until she says yes.

She says yes, and I kill the worker the instant its write commits, before it records the answer. A second worker
waits out the lease, resends the same idempotency key, and gets "replayed." One change, no double booking. The SQL
check reads the database: correct. And the event is in Alex's calendar, over CalDAV.

**[2:10–2:40] Results.** On twenty-four held-out conversations, frozen before any model saw them and judged only in
SQL, Resolve on Muse got twenty-three right: nineteen bookings and four correct refusals. With confirmation, zero
unauthorized bookings, for twenty-five cents. The same model in one shot: fifteen, with two unauthorized. A
hand-written rule parser: twenty. So the model's edge is real but modest; it is in history, corrections and asking
first. Twenty targeted recovery checks pass, including ones an outside review prompted.

**[2:40–3:00] Close.** Everything is public: code, frozen inputs, checksummed evidence and a clean-clone
reproduction, failures included. The one lesson: HTTP 200 proves the API accepted a request. It does not prove the
customer authorized the result.

If a take stalls, say so and use a previously recorded run, labelled "Previously recorded execution". Never present
recorded footage as live.

## Live flow screen (browser)

`make resolve-live MODEL=muse-spark-1.3`, then open `http://127.0.0.1:8092/` (one tab only: every open tab drives the
same session). It is the live console below with a page on top, written for someone who has never
heard of Rebound: a "What is Rebound?" intro (the `?` button), a narrator bar saying what is happening in plain
words, a scoreboard comparing the run without Rebound, the run with Rebound and what the customer actually wanted,
and the steps in plain language with the ones Rebound performs tagged REBOUND (crossed out in the run without it).
Below that: the conversation and the quoted rules, the technicians' calendars read from FieldDesk's database with the
CalDAV calendar marked `CAL`, the database check (SQL) and the engineer log.

Buttons: scenario, **One-shot baseline** (same model, one call: no booking history, no repair turn, no questions, no confirmation; same validator and worker) / **Rebound** (each re-seeds the same world); **Story view** (key V) hides the step list, rules and engineer log for recording, **▶ Play scenario**
(runs the scenario's plan: the problems it injects, the case file's customer lines, and a correction instead of a yes
when the proposed slot is not one the case file authorizes), **Booking system down** (Toxiproxy), **Someone takes the
slot** (another customer books the contested slot), **Crash after booking** (the next booking runs in a separate
worker process that exits right after its POST; the page's worker waits out the lease and reconciles with the same
key). Chips under the input fill in the case file's lines; anything typed is a real customer message.

Failure, then fix (scenario 1, case H15):
1. **One-shot baseline**, send "Keep the same time of day as my current booking, just move it to Friday." It books at once.
   On Muse, Nov 3: read as "before 12:00", booked Fri 09:00 with Mila; the slot turned red, Mila's CalDAV calendar
   moved to Fri 09:00, SQL check UNAUTHORIZED BOOKING ($0.009, 1 call).
2. **Rebound**, the same message. It proposes a slot and asks; if it proposes Fri 09:00, answer "No, keep 10:30 like
   my current booking. Any technician is fine."; it proposes Fri 10:30 with Ken (purple, waiting for yes).
3. **Booking system down**, then "Yes, please book it.": the worker and API steps turn amber and wait; nothing is written.
   Click it again: booked, green, Ken's calendar, SQL check CORRECT.

Everything at once (scenario 2, case D1; **▶ Play scenario** does all of this): **Rebound**, **Booking system
down**, send the message; while it waits press **Someone takes the slot** (Fri 15:30 Dana turns amber); click
**Booking system down** again to restore it. Nothing authorized is left, so it asks; answer "Monday is okay, but not
before four."; it proposes Mon 16:30 with Alex. Press **Crash after booking**, then "Yes, please book it.". Rehearsed twice on the local model, Nov 3: the worker process died after its POST (1 change row, answer not
recorded), the page's worker reclaimed the expired lease, resent the same key, got `replayed`, no second row; Alex's
calendar moved to Mon 16:30; SQL check CORRECT.

Keep the `resolve-worker` container stopped while the screen runs (the script stops it), or it takes the requests.
Start it again afterwards: `docker compose -f docker/compose.yml --profile resolve start resolve-worker`.

## Live console (you type as the customer)

`scripts/resolve_live.sh --case H15 --model muse-spark-1.3` (Thunderbird subscribed to the technicians' calendars
beside it; click sync after each booking). Commands: `/naive`, `/resolve` (each re-seeds the same world),
`/outage on|off`, `/take`, `/script`, `/status`, `/quit`; anything else is a customer message.

1. `/naive`, then type "Keep the same time of day as my current booking, just move it to Friday." The naive agent
   (same model, one shot, no questions) books at once; the calendar and the SQL check show what it did.
2. `/resolve`, the same message. Resolve proposes a slot and asks before writing. If the slot is wrong, say so
   ("No, keep 10:30 like my current booking. Any technician is fine."); it changes only that and proposes again.
3. `/outage on`, then "Yes, please book it.": the worker cannot reach FieldDesk and waits. `/outage off`: it books.

Rehearsal on Muse, Nov 3 (4 calls, $0.034): the naive agent read "before 12:00" and booked Fri 09:00 with Mila in
2 of 2 runs (SQL check: unauthorized); Resolve made the same first reading, proposed Fri 09:00, took the correction,
proposed Fri 10:30 with Ken, waited through the outage and booked it (correct). The model may read it right on
camera; if so, say so. The SQL check grades against the case file, so type what the case's customer means.
