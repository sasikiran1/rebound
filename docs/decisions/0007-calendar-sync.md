# 0007 Technicians' calendars mirrored over CalDAV

**Status:** accepted, Nov 3, 2026.

**Context.** A booking that exists only in FieldDesk's tables is invisible to the technician who has to turn up.
Field teams live in their calendars, and CalDAV is the open protocol most calendar servers speak (Nextcloud, Fastmail,
iCloud, Radicale; Google exposes it too, behind OAuth).

**Decision.** FieldDesk stays the source of truth; calendars are a mirror. Completing a request writes a
`resolve.calendar_sync` row in the same transaction (`Store.set_state`). The worker's service loop drains due rows:
it reads where FieldDesk says the appointment is now, PUTs one event per appointment (`appointment-<id>.ics`) into the
assigned technician's calendar, and DELETEs copies from the other technicians' calendars in the region. Failures stay
queued with exponential backoff and are marked failed after six attempts. Locally the server is Radicale with one
basic-auth login (`resolve-calendar` in compose, `resolve/eval/calserver.py`).

**Why a mirror, not a source.** Reading free/busy time from the calendars to restrict bookings would change which
slots are authorized, and with it every frozen result; the mirror changes nothing the oracle judges.

**Consequences.** Every step is idempotent, so a retry or a crash between steps only repeats work; a calendar outage
delays the event but never the booking (checked: `calendar` in the recovery suite, with the server stopped during
the booking). FieldDesk has no appointment length and local times only, so events are floating local times of 60
minutes (`RESOLVE_CALDAV_EVENT_MIN`). Not done: a hosted provider (configuration only for basic-auth CalDAV; Google
needs OAuth), free/busy reads, and calendar edits flowing back into FieldDesk.
