# Three-minute demo script

Everything shown is a measured result or a live command. Recorded results are labeled as recorded; only segment 2
is live. Before recording: `make sut-up`, open `runs/eval-v1/report.html` in a browser tab, open a terminal in
`~/rebound` (WSL), and check spend (segment 2 costs about $0.01 of the remaining $0.19).

| Time | Segment | On screen | Words |
|---|---|---|---|
| 0:00–0:20 | The question | README title and the one-line question | "AI agents now call APIs. When a database hiccups, do they make recovery worse, and does an admission gate help without losing their work? Rebound measures it on a real API with real agents." |
| 0:20–0:55 | **Live**: one Muse Spark 1.3 agent does one task | `make goal TASK=A0001 CONTINUATION=full_history` (task set A; the evaluation's continuation mode) → tool calls stream, then the SQL oracle line `correct` | "A live Meta Muse Spark agent reschedules an appointment through seven tools. No model grades it: an SQL oracle reads the database and checks date, service, skill, region and that exactly one change was made." |
| 0:55–1:30 | Recorded: the frozen evaluation | `report.html`: recovery-time chart and lost-service chart, gate off vs on | "18 trials, frozen before the first one ran. After a 10 s database slowdown the API recovers on its own in about 20 s; with the gate, at once. Ordinary users lose about 3,000 fewer requests. Agents' correct work: 209 vs 208." |
| 1:30–2:05 | Recorded: what agents actually do | `python -c` one-liner printing a back-pressure witness from `runs/eval-v1/tr_0004/reanalysis/contract.json` (goal, refused attempt, gap) | "When the gate says 'retry after 2 seconds', the agents sent their next request early every time: 69 of 69, 74 of 74, 68 of 68. The scripted client: 3 of 23. In a 20 s fault this cost the agents 8 of 209 goals." |
| 2:05–2:35 | **Live**: the oracle catches a wrong-but-successful booking | `python scripts/oracle_fixture_check.py` (real FieldDesk writes, no model) | "In a separate local-model test, one model booked 8 a.m. for a customer who said 'after 3 p.m.'. The API said 200 OK; the old oracle said correct. The oracle now checks the customer's stated constraint: 08:00 incorrect, 15:00 correct." |
| 2:35–3:00 | **Live**: the evidence cannot be faked | `scripts/tamper_demo.sh runs/eval-v1/tr_0004` (works on a copy) | "Every number re-derives from checksummed raw logs without a model key. Change one word in a copy of the evidence and verification refuses it. Limits: one API, one fault type, one laptop; agents were a small share of load, so we do not claim they slow recovery." |

Back-pressure witness one-liner (segment 4):

```bash
python3 -c "import json; c=json.load(open('runs/eval-v1/tr_0004/reanalysis/contract.json'))['predicates']['backpressure']; print(c['reason']); print(json.dumps(c['witnesses'][0], indent=1))"
```

Fallbacks: if the live goal stalls, show the recorded goal from `runs/eval-v1/tr_0001` and say "recorded". Never label
recorded material as live.
