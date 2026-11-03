# Resolve cases

Synthetic fixtures with scripted customer turns, written by the builder. `dev.yaml` is for prompt development;
`heldout.yaml` was committed before any model saw it and is not used for tuning. `faults.yaml` drives the recovery
checks.

Fixture clock: today is **Tuesday 2026-11-10**; bookable horizon 13 days (to 2026-11-23).
Day tokens: `tue`=11-10, `wed`=11-11, `thu`=11-12, `fri`=11-13, `sat`=11-14, `mon2`=11-16, `tue2`=11-17,
`wed2`=11-18, `thu2`=11-19, `fri2`=11-20.

Each case is seeded into its own region of the trial database (its own technicians, customer, history, current
appointment and open slots), so the real FieldDesk API serves exactly the slots listed.

| field | meaning |
|---|---|
| `messages` | customer turns delivered unprompted (the first opens the request; later ones are corrections) |
| `replies` | customer answers, delivered in order, one per question the system asks |
| `taken` | open slots another customer books after the request is stored and before it is executed |
| `expected.authorized` | per customer turn k (0 = first message): every open slot the customer has authorized by then, given availability |
| `expected.final` | slots that are a correct final booking; `[]` means the correct outcome is no booking |
| `expected.preferred` | optional subset of `final` that also meets stated preferences |
| `expected.questions` | questions an ideal assistant asks (`null`: not scored) |
| `form` | what a careful user enters in the capable conditional form (same rule vocabulary); `null`: the user cannot complete it |
| `form_turns` | the user's form state after each later turn (full form, `decline: true`, or `unchanged: true`) |

The expected sets are enumerated slot by slot from the customer's words. They do not use the model's output or the
rule vocabulary, so a wrong interpretation cannot grade itself.
