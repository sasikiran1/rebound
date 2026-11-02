# Three-minute demo script

Segments 2 and 5 are live; everything else is recorded data shown in the results page, and the narration says so.

Before recording: run `make sut-up`, open `ui/index.html` in a browser, and open a terminal in the repository.
Segment 2 makes one live Muse call, about $0.01.

| Time | Segment | On screen | Words |
|---|---|---|---|
| 0:00–0:20 | The question | **Overview**, title and the four numbers | "AI agents now call APIs. When a database slows down, do they make recovery worse? Does an admission gate help, and what does it cost them? Rebound measures this on a real API with real agents." |
| 0:20–0:50 | **Live:** one agent, one task | Terminal: `make goal TASK=A0001 CONTINUATION=full_history` | "A live Meta Muse Spark agent reschedules an appointment through seven tools. No model grades it. An SQL oracle reads the database and checks every condition." |
| 0:50–1:20 | Recorded: recovery | **Recovery**, 20 s fault, then "Slowest 5% of requests", then "Ordinary users served" | "Eighteen trials, frozen before the first one ran. In a 20-second fault the API takes 34 seconds to recover alone, and 10 with the gate. Lost requests halve. Note that the low latency during the fault is refused requests, not service." |
| 1:20–1:50 | Recorded: what agents do | **Agent behavior**, table, then the step-by-step goal | "The gate says retry after 2 seconds. The agents ignored that every time: 350 of 350. This goal was refused 13 times and ran out of tools. In the 20-second fault, agents lost 8 of 209 goals." |
| 1:50–2:25 | Recorded: the fix | **Fixes**, table and the met and not-met marks | "So we fixed the agent. A runtime guard waits out Retry-After. Nothing went early, and no goal was lost: 209 of 209. We also show what missed: refusals fell less than the bar we set, the guard didn't speed recovery, and plain-language handling gained only one goal because Muse was already good at it." |
| 2:25–2:40 | Recorded: the wrong booking | **Bookings**, the table | "In a separate local-model test, one model booked 8 a.m. for a customer who said 3 p.m. or later. The API returned 200 and the old oracle passed it. A start-time filter fixed 8 of 8 cases, and the oracle now checks stated constraints." |
| 2:40–3:00 | **Live:** evidence can't be faked | Terminal: `scripts/tamper_demo.sh runs/eval-v1/tr_0004` (works on a copy) | "Every number re-derives from checksummed logs, with no model key. Change one word in a copy and verification refuses it. Limits: one API, one fault type, one laptop. Agents were a small share of load, so we don't claim they slow recovery." |

If the live goal stalls, say so and move on to the results page. Never present recorded data as live.
