"""Turn a user's tasks into an iCalendar (.ics) feed that Google Calendar can subscribe to.

An .ics file is just text in a fixed format (RFC 5545):

    BEGIN:VCALENDAR
    ...
    BEGIN:VEVENT
    UID:task-57@deadline-buddy
    DTSTART;VALUE=DATE:20261025
    SUMMARY:physics test
    END:VEVENT
    END:VCALENDAR
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

INDIA = ZoneInfo("Asia/Kolkata")


def escape(text):
    # Commas, semicolons and backslashes mean something in .ics, so they get a backslash.
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
                .replace("\r", "").replace("\n", "\\n"))


def fold(line):
    """Lines may be at most 75 bytes. Longer ones continue on the next line after a space."""
    parts, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))  # emoji are 4 bytes, so count bytes, not letters
        if size + n > 75:
            parts.append(current)
            current, size = " ", 1
        current += ch
        size += n
    parts.append(current)
    return "\r\n".join(parts)


def utc_stamp(due, due_time):
    """India date + time -> UTC text like 20261023T113000Z (5:00 PM IST = 11:30 UTC)."""
    start = datetime.combine(due, due_time, tzinfo=INDIA).astimezone(timezone.utc)
    return start.strftime("%Y%m%dT%H%M%SZ")


def make_ics(rows, now_utc):
    """rows: [(task_id, task, due_date, due_time_or_None), ...]   now_utc: when this feed was made."""
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Deadline Buddy//Telegram bot//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Deadline Buddy",
        "X-WR-TIMEZONE:Asia/Kolkata",
        # A hint for calendar apps to check back every hour (Google mostly ignores it).
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for task_id, task, due, due_time in rows:
        if due_time is None:
            # VALUE=DATE (no time) = an all-day event. DTEND is the day *after*.
            when = [f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
                    f"DTEND;VALUE=DATE:{due + timedelta(days=1):%Y%m%d}"]
        else:
            # A timed event, written in UTC (the Z at the end). One hour long.
            end = datetime.combine(due, due_time) + timedelta(hours=1)
            when = [f"DTSTART:{utc_stamp(due, due_time)}",
                    f"DTEND:{utc_stamp(end.date(), end.time())}"]
        lines += [
            "BEGIN:VEVENT",
            # The UID never changes for a task, so the calendar updates it instead of making a copy.
            f"UID:task-{task_id}@deadline-buddy",
            f"DTSTAMP:{stamp}",
            *when,
            f"SUMMARY:{escape(task)}",
            "DESCRIPTION:From Deadline Buddy on Telegram",
            "TRANSP:TRANSPARENT",  # don't show you as "busy" all day
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"
