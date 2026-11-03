# 0003 The model never sees availability and never books

**Status:** accepted, Nov 3, 2026.

**Context.** Agents with booking tools booked slots that broke the customer's stated limits while the API returned
200 OK (study: Muse booked 10:00 for "6 pm only"). Letting the model choose slots also invites invented slots.

**Decision.** The model's only output is a typed intent (or a patch to one), each rule quoting the customer's words.
It sees booking history, the technician roster and a calendar, not open slots. A deterministic evaluator matches
the intent against every open slot; the worker writes.

**Consequences.** The model cannot invent a slot or book without authority. It can still misread words it quotes
correctly (held-out H15: "same time of day as my current booking" as "morning"); the validator cannot see that, so
decision 0004 adds the customer as the last check. Matching quality is identical for every arm, so comparisons
measure interpretation only.
