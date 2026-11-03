from resolve.calendar import CalDAV, event_ics

APPT = {"appointment_id": 805200, "customer_id": 900052, "service": "plumbing_repair", "starts_at": "2026-11-12T11:00",
        "technician_id": 900521}


def test_event_is_local_time_with_default_length():
    ics = event_ics(APPT, "Dana", "r1", 77)
    assert "UID:appointment-805200@rebound\r\n" in ics
    assert "DTSTART:20261112T110000\r\n" in ics
    assert "DTEND:20261112T120000\r\n" in ics
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")


def test_event_length_and_text_escaping():
    ics = event_ics(APPT, "Lee, Jr.; on call", "run:case:check", None, minutes=90)
    assert "DTEND:20261112T123000\r\n" in ics
    assert "Lee\\, Jr.\\; on call" in ics
    assert "FieldDesk change" not in ics


def test_one_calendar_per_technician_and_one_event_per_appointment():
    c = CalDAV("http://cal.example/fielddesk")
    assert c.calendar_url(7) == "http://cal.example/fielddesk/tech-7/"
    assert c.event_url(7, 805200) == "http://cal.example/fielddesk/tech-7/appointment-805200.ics"
