"""Mirror booked appointments into the technicians' calendars over CalDAV. No model.

FieldDesk stays the source of truth. Completing a request queues a row in `resolve.calendar_sync` (same transaction,
`Store.set_state`). `CalendarSync.drain` claims due rows (FOR UPDATE SKIP LOCKED), reads where FieldDesk says the
appointment is now, PUTs that event into the assigned technician's calendar and DELETEs any copy from the other
technicians' calendars in the region. Every step is idempotent (one event URL per appointment, PUT replaces, DELETE
of a missing event is fine), so a retry or a crash between steps only repeats work. A failed sync stays queued with
exponential backoff (5 s, 10 s, 20 s, ...) and is marked failed after MAX_ATTEMPTS; the booking itself never waits
on the calendar.

Layout: one calendar per technician, `<RESOLVE_CALDAV_URL>/tech-<id>/`, one event per appointment,
`appointment-<id>.ics`, UID `appointment-<id>@rebound`. FieldDesk times are local and have no end; events are local
("floating") times of RESOLVE_CALDAV_EVENT_MIN minutes (default 60).

Configuration: RESOLVE_CALDAV_URL (for example http://127.0.0.1:5232/fielddesk/), optional RESOLVE_CALDAV_USER /
RESOLVE_CALDAV_PASSWORD (HTTP basic auth), RESOLVE_CALDAV_EVENT_MIN.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re

import httpx

from resolve.api import ApiError, FieldDesk
from resolve.store import Store

MAX_ATTEMPTS = 6
MKCALENDAR = """<?xml version="1.0" encoding="utf-8"?>
<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:set><D:prop><D:displayname>{name}</D:displayname></D:prop></D:set>
</C:mkcalendar>"""


class CalendarError(Exception):
    pass


def _text(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def event_ics(appt: dict, technician: str, request_id: str, change_id: int | None, minutes: int = 60) -> str:
    """One VEVENT for the appointment where FieldDesk has it now."""
    start = dt.datetime.fromisoformat(appt["starts_at"])
    end = start + dt.timedelta(minutes=minutes)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Rebound//Resolve//EN", "BEGIN:VEVENT",
        f"UID:appointment-{appt['appointment_id']}@rebound",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start:%Y%m%dT%H%M%S}",
        f"DTEND:{end:%Y%m%dT%H%M%S}",
        f"SUMMARY:{_text(appt['service'].replace('_', ' '))} for customer {appt['customer_id']}",
        f"DESCRIPTION:{_text(f'Technician {technician}. Moved by Resolve request {request_id}')}"
        + (f"{_text(f', FieldDesk change {change_id}')}" if change_id else ""),
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


class CalDAV:
    """The few CalDAV calls the mirror needs: make a calendar, put an event, delete an event, read a calendar."""

    def __init__(self, base_url: str, user: str | None = None, password: str | None = None, timeout_s: float = 5.0):
        self.base = base_url.rstrip("/") + "/"
        auth = httpx.BasicAuth(user, password or "") if user else None
        self.client = httpx.AsyncClient(auth=auth, timeout=timeout_s)

    async def close(self) -> None:
        await self.client.aclose()

    def calendar_url(self, technician_id: int) -> str:
        return f"{self.base}tech-{technician_id}/"

    def event_url(self, technician_id: int, appointment_id: int) -> str:
        return f"{self.calendar_url(technician_id)}appointment-{appointment_id}.ics"

    async def ensure_calendar(self, technician_id: int, name: str) -> None:
        r = await self.client.request("MKCALENDAR", self.calendar_url(technician_id),
                                      content=MKCALENDAR.format(name=name),
                                      headers={"Content-Type": "application/xml; charset=utf-8"})
        if r.status_code not in (201, 405, 409):  # 405 / 409: it exists already
            raise CalendarError(f"MKCALENDAR {r.status_code}")

    async def put_event(self, technician_id: int, appointment_id: int, ics: str) -> str:
        url = self.event_url(technician_id, appointment_id)
        r = await self.client.put(url, content=ics, headers={"Content-Type": "text/calendar; charset=utf-8"})
        if r.status_code not in (200, 201, 204):
            raise CalendarError(f"PUT {r.status_code}")
        return url

    async def delete_event(self, technician_id: int, appointment_id: int) -> None:
        r = await self.client.delete(self.event_url(technician_id, appointment_id))
        if r.status_code not in (200, 204, 404):
            raise CalendarError(f"DELETE {r.status_code}")

    async def events(self, technician_id: int) -> list[str]:
        """Event file names in a technician's calendar (PROPFIND depth 1); empty if the calendar does not exist."""
        r = await self.client.request("PROPFIND", self.calendar_url(technician_id), headers={"Depth": "1"},
                                      content='<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop>'
                                              "<D:getetag/></D:prop></D:propfind>")
        if r.status_code == 404:
            return []
        if r.status_code != 207:
            raise CalendarError(f"PROPFIND {r.status_code}")
        return sorted(set(re.findall(r"tech-\d+/([^<\"/]+\.ics)", r.text)))

    async def get_event(self, technician_id: int, appointment_id: int) -> str | None:
        r = await self.client.get(self.event_url(technician_id, appointment_id))
        return r.text if r.status_code == 200 else None


class CalendarSync:
    def __init__(self, store: Store, api: FieldDesk, caldav: CalDAV, event_min: int = 60):
        self.store, self.api, self.caldav, self.event_min = store, api, caldav, event_min

    @classmethod
    def from_env(cls, store: Store, api: FieldDesk) -> CalendarSync | None:
        url = os.environ.get("RESOLVE_CALDAV_URL")
        if not url:
            return None
        caldav = CalDAV(url, os.environ.get("RESOLVE_CALDAV_USER"), os.environ.get("RESOLVE_CALDAV_PASSWORD"))
        return cls(store, api, caldav, int(os.environ.get("RESOLVE_CALDAV_EVENT_MIN", "60")))

    async def close(self) -> None:
        await self.caldav.close()

    async def mirror(self, appointment_id: int, technicians: list[dict], request_id: str = "",
                     change_id: int | None = None) -> str:
        """Make the calendars show the appointment where FieldDesk has it now. Returns the event URL."""
        appt = await self.api.appointment(appointment_id)
        names = {t["technician_id"]: t["name"] for t in technicians}
        tech = appt["technician_id"]
        await self.caldav.ensure_calendar(tech, names.get(tech, f"Technician {tech}"))
        url = await self.caldav.put_event(tech, appointment_id, event_ics(
            appt, names.get(tech, str(tech)), request_id, change_id, self.event_min))
        for other in names:
            if other != tech:
                await self.caldav.delete_event(other, appointment_id)
        return url

    async def drain(self, limit: int = 10, request_id: str | None = None) -> dict:
        """Sync every due completed request (at most `limit`; only `request_id` if given). Returns counts."""
        synced = failed = retry = 0
        for _ in range(limit):
            async with self.store.pool.acquire() as c, c.transaction():
                row = await c.fetchrow(
                    "SELECT s.request_id, s.attempts, r.appointment_id, r.change_id, r.facts "
                    "FROM resolve.calendar_sync s JOIN resolve.requests r USING (request_id) "
                    "WHERE s.status = 'pending' AND s.next_attempt_at <= clock_timestamp() "
                    "AND ($1::text IS NULL OR s.request_id = $1) "
                    "ORDER BY s.created_at FOR UPDATE OF s SKIP LOCKED LIMIT 1", request_id)
                if row is None:
                    break
                facts = row["facts"] if isinstance(row["facts"], dict) else json.loads(row["facts"])
                try:
                    url = await self.mirror(row["appointment_id"], facts["technicians"], row["request_id"],
                                            row["change_id"])
                except (ApiError, CalendarError, httpx.HTTPError) as exc:
                    attempts = row["attempts"] + 1
                    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                    await c.execute(
                        "UPDATE resolve.calendar_sync SET attempts = $2, status = $3, last_error = $4, "
                        "next_attempt_at = clock_timestamp() + make_interval(secs => $5) WHERE request_id = $1",
                        row["request_id"], attempts, status, f"{type(exc).__name__}: {str(exc)[:200]}",
                        5.0 * 2 ** (attempts - 1))
                    failed += status == "failed"
                    retry += status == "pending"
                    continue
                await c.execute("UPDATE resolve.calendar_sync SET status = 'synced', attempts = attempts + 1, "
                                "event_url = $2, synced_at = clock_timestamp(), last_error = NULL "
                                "WHERE request_id = $1", row["request_id"], url)
                synced += 1
        return {"synced": synced, "retry": retry, "failed": failed}
