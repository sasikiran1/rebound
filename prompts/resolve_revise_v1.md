A field-service customer already has a structured intent for moving their appointment. They have just sent a new message, possibly answering a question we asked. Express exactly what the new message changes as operations on the intent.

Call the tool `revise_intent` exactly once. Do not reply with text.

Every rule has an id (r1, r2, ...). Rules you do not mention stay exactly as they are, so mention only what the new message changes.

Operations:
- `accept_proposal` with `slot_id`: the customer accepts one of the proposed slots exactly as offered.
- `replace_rule` with `rule_id` and a full new `rule`: the message changes that rule (for example widens a date range to include a day the customer now allows).
- `add_rule` with `rule` (and `alternative` number to add it to one alternative): a new requirement.
- `remove_rule` with `rule_id`: the customer drops that requirement.
- `add_alternative` with `label` and `rules`: a new acceptable option, used only if every other option fails. Top-level rules still apply to it.
- `remove_alternative` with `alternative` (1-based).
- `choose_reading` with `unclear_index` and `reading_index` (0-based): the customer says which meaning of an unclear phrase they meant.
- `decline`: the customer refuses the options or does not want the change made, or does not choose now.

Rules use the same kinds, fields and strengths as the intent: `must` if the customer says it cannot change, `flexible` for a requirement, `prefer` for a wish. Each rule's `quote` must be the customer's exact words from any of their messages. To limit a new requirement to the day it was said about ("Monday is okay, but not before four"), set `only_on_dates` to that date. If the customer allows a date that a rule excludes, replace that rule so the date is allowed; keep its other limits.

Use the calendar in the facts for dates. "morning" = before 12:00, "afternoon" = 12:00 or later, "after N" = start_at_or_after N.

`summary`: one plain sentence of what the customer now wants.
