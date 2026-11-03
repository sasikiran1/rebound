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
