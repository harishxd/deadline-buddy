"""Check that our .ics feed is valid by reading it back with the `icalendar` package.
Run with:  python test_ics.py      (needs: pip install -r requirements-dev.txt)"""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
from icalendar import Calendar
from icsfeed import make_ics

ROWS = [
    (1, "physics test", date(2026, 10, 25), None),
    (2, "chem record, part 2; with notes \\ backslash", date(2026, 10, 23), None),
    (3, "📚 a very long task name with emoji that is definitely longer than seventy five bytes 🎉🎉", date(2026, 12, 31), None),
]
TIMED = (4, "call mom", date(2026, 10, 22), time(17, 0))           # 5:00 PM India time
LATE = (5, "submit before midnight", date(2026, 10, 22), time(23, 30))  # ends the next day


def test_feed_is_valid():
    text = make_ics(ROWS, datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc))

    # Every line must be at most 75 bytes and end with CRLF.
    assert text.endswith("\r\n")
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line

    cal = Calendar.from_ical(text)
    events = cal.walk("VEVENT")
    assert len(events) == 3
    assert str(cal["X-WR-CALNAME"]) == "Deadline Buddy"

    for event, (task_id, task, due, _) in zip(events, ROWS):
        assert str(event["UID"]) == f"task-{task_id}@deadline-buddy"
        assert str(event["SUMMARY"]) == task          # escaping/folding undone correctly
        assert event["DTSTART"].dt == due             # a date, not a datetime = all-day
        assert (event["DTEND"].dt - due).days == 1

    # Dec 31 ends on Jan 1 of the next year
    assert events[2]["DTEND"].dt == date(2027, 1, 1)


def test_timed_events():
    cal = Calendar.from_ical(make_ics([TIMED, LATE], datetime.now(timezone.utc)))
    call, late = cal.walk("VEVENT")
    india = ZoneInfo("Asia/Kolkata")
    # Stored in UTC, but it must be 5:00 PM when shown in India
    assert call["DTSTART"].dt.astimezone(india) == datetime(2026, 10, 22, 17, 0, tzinfo=india)
    assert (call["DTEND"].dt - call["DTSTART"].dt).total_seconds() == 3600
    assert late["DTEND"].dt.astimezone(india) == datetime(2026, 10, 23, 0, 30, tzinfo=india)


def test_empty_feed_is_valid():
    cal = Calendar.from_ical(make_ics([], datetime.now(timezone.utc)))
    assert cal.walk("VEVENT") == []


if __name__ == "__main__":
    test_feed_is_valid()
    test_timed_events()
    test_empty_feed_is_valid()
    print("ICS feed tests passed ✅")
