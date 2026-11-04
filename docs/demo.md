# Three-minute demo script

Two windows side by side: the Resolve screen (`http://127.0.0.1:8090/?rid=<run>:D1:resolve`) and a terminal. Segment 2
is live on Muse (2 model calls, about $0.02, capped by the script); everything else is recorded data and the
narration says so. Burn in a caption on every segment, because judges may watch muted: "synthetic data, scripted
customer".

Before recording: `make sut-up`; `docker compose -f docker/compose.yml --profile resolve up -d resolve-calendar`
(the technicians' calendars; keep the `resolve-worker` container stopped, or it takes the demo's request); start the
screen with `RESOLVE_CALDAV_URL=http://127.0.0.1:5232/fielddesk/ RESOLVE_CALDAV_USER=fielddesk
RESOLVE_CALDAV_PASSWORD=local-only uv run uvicorn resolve.app:app --port 8090`; open `docs/resolve_results.md` and
`ui/index.html` in tabs. The demo prints the calendar before and after; the screen's "Technician calendar" panel
reads it back over CalDAV.

| Time | Segment | On screen | Words |
|---|---|---|---|
| 0:00–0:15 | The problem | Split card: left "Agent: Done! You're booked. API: 200 OK" in green; right "Customer: 6 pm only. SQL: booked 10:00. INCORRECT" in red (recorded Muse run, plain-language study, MI04) | "An AI agent said done. The API said 200 OK. The customer had said 6 pm only. Only an independent database check noticed." |
| 0:15–0:35 | What we measured | `ui/index.html`, agent-behavior table | "We ran real Muse Spark agents against a scheduling API while we slowed its database. They retried before Retry-After every single time. A runtime guard fixed it with no lost work. And our own thesis failed: at our traffic mix, agents did not measurably slow recovery. We report that." |
| 0:35–2:05 | **Live:** Resolve | Terminal `scripts/resolve_demo_muse.sh demo-video`, screen beside it | "A customer writes: after three this week, Dana if possible, check with me before next week. Muse turns it into rules, each quoting her words. Now the database link slows down: the booking system times out, the request waits. Meanwhile someone takes Friday with Dana. The system recovers. Nothing she authorized is left, so it asks, offering only real open slots. She answers: Monday is okay, but not before four. Version 2 changes only that, and only for Monday. It asks her to confirm the exact slot. Now we kill the worker right after the booking is written. A new worker reclaims the job, resends with the same key, gets 'replayed', no second booking. The SQL check: one change, the slot she approved. And it is in Alex's calendar, over CalDAV." |
| 2:05–2:35 | Recorded: held-out results | `docs/resolve_results.md`, top table | "Twenty-four conversations frozen before any model saw them. Muse with grounding and questions: 23 correct, one unauthorized booking, about 23 cents. The same model in one shot: 15. A form filled in perfectly: 24, so the engineering is sound and the model's value is in history, corrections and asking first." |
| 2:35–3:00 | Limits | One slide | "The miss: 'same time as my current booking' read as 'morning'. Confirm mode is the guard. A customer who taps a link should get a slot picker, not a chatbot. A hand-written rule parser got 20 of 24, so the model's edge is real but modest. Synthetic data, one laptop, a local calendar server and test inbox, no real users yet. Every number is in the repo." |

If the live run stalls, say so and switch to the recorded run `runs/resolve/demo/demo-muse-1`. Never present recorded
data as live.

## Live flow screen (browser)

`scripts/resolve_live_ui.sh --model muse-spark-1.3`, then open `http://127.0.0.1:8092/`. It is the live console
below with a page on top: one screen with the conversation and the quoted rules, the pipeline (each step lights up
when its event or row exists), the technicians' schedule read from FieldDesk's database with the CalDAV calendar
marked `CAL`, the SQL check and the system log. Buttons: scenario, **Naive agent** / **Resolve** (each re-seeds the
same world), **DB outage** (Toxiproxy), **Slot taken** (another customer books the contested slot), **Kill worker
after write** (the next booking runs in a separate worker process that exits right after its POST; the page's worker
waits out the lease and reconciles with the same key), **Play script** (sends the case file's lines, and yes to a
confirmation). Chips under the input fill in the case file's lines; anything typed is a real customer message.

Failure, then fix (scenario C, case H15):
1. **Naive agent**, send "Keep the same time of day as my current booking, just move it to Friday." It books at once.
   On Muse, Nov 3: read as "before 12:00", booked Fri 09:00 with Mila; the slot turned red, Mila's CalDAV calendar
   moved to Fri 09:00, SQL check UNAUTHORIZED BOOKING ($0.009, 1 call).
2. **Resolve**, the same message. It proposes a slot and asks; if it proposes Fri 09:00, answer "No, keep 10:30 like
   my current booking. Any technician is fine."; it proposes Fri 10:30 with Ken (purple, waiting for yes).
3. **DB outage** on, then "Yes, please book it.": the worker and API steps turn amber and wait; nothing is written.
   **DB outage** off: booked, green, Ken's calendar, SQL check CORRECT.

Everything at once (scenario A, case D1): **Resolve**, **DB outage** on, send the message; while it waits press
**Slot taken** (Fri 15:30 Dana turns amber); **DB outage** off. Nothing authorized is left, so it asks; answer "Monday
is okay, but not before four."; it proposes Mon 16:30 with Alex. Press **Kill worker after write**, then "Yes, please
book it.". Rehearsed twice on the local model, Nov 3: the worker process died after its POST (1 change row, answer not
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
