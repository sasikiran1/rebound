# 0004 Confirm before booking

**Status:** accepted for the product, Nov 3, 2026; off in the frozen evaluation.

**Context.** Validation catches invented facts, not wrong meanings. Critics asked why a customer should trust a
model-made booking at all, and why the assistant promised bookings it could not yet make.

**Decision.** Per request flag `confirm` (service default on). Before any write the worker asks "I can move you to
<slot>. Shall I book it? Your current appointment stays as it is until you say yes." A plain yes is matched exactly
(no model call) and approves that slot as a new version; anything else goes to the model as a change. Replies never
promise a booking that does not exist.

**Consequences.** One extra customer turn per booking. The 24 held-out cases were written and frozen without it, so
it is not used there; it is demonstrated (demo, `confirm_mode` check, the HTTP end-to-end test) but its effect on the
held-out error (H15) is argued, not measured.
