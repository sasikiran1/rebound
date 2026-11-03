# 0005 Retries belong in middleware, not in a model tool

**Status:** accepted, Nov 3, 2026.

**Context.** The study gave agents a `wait` tool: early retries fell from 60/60 to 0/60. Each resume, though, is a
full model call, and a client guard that honors Retry-After achieved 0 early with 209/209 correct goals and no model
calls.

**Decision.** Resolve never shows a 503 to the model. Short failures are handled in code; a request that cannot
finish is released to `ready` and the worker retries later, with no model involved. The model is called again only
when the customer must decide something.

**Consequences.** The `wait` tool stays in the study as a finding about agent behavior, not as a recommended pattern.
