# 0006 Customer notifications from a transactional queue

**Status:** accepted, Nov 3, 2026.

**Context.** Resolve stored its questions and outcomes but sent nothing; a customer who had left the chat would never
learn that their first choice was gone, or that nothing was booked. Critics called this a recipe for silent drops.

**Decision.** `Store.add_turn` writes a notification row in the same transaction as every assistant turn the customer
must see (questions, confirmations, completed, unresolved). The worker's service loop delivers due rows over SMTP,
marks them sent, and keeps failures queued with exponential backoff, marking them failed after six attempts. Each
message has a stable Message-ID (`<request>.<turn>@resolve>`), because a crash between the SMTP answer and the update
can send it twice.

**Consequences.** A question cannot exist without its message, and a mail outage delays messages instead of losing
them (checked: `notifications` in the recovery suite). Locally the recipient is a test inbox and addresses are
synthetic; replies by email are not read back, so answering still happens through the intake API. A real provider
(and SMS) is a configuration and adapter change, not done here.
