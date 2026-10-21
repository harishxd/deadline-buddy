"""Find a deadline inside a normal sentence.

    extract_date("chem record friday", today)  ->  ("chem record", date(2026, 10, 23))
    extract_date("chapter 5 notes", today)     ->  ("chapter 5 notes", None)

We only look for a small list of date shapes (patterns below). Anything that
doesn't look like one of them is left alone, so "lab 3" or "may i ..." never
turn into a wrong date.
"""
import re
from datetime import date, time, timedelta

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _either(words):
    # Longest first, so "september" is tried before "sep".
    return "|".join(sorted(words, key=len, reverse=True))


MONTH = r"(?P<mon>" + _either(MONTHS) + r")(?![a-z])\.?"
DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
YEAR = r"(?:,?\s*(?P<year>\d{4}))?"
# "sat" and "sun" are normal words too ("SAT exam", "sun chapter"), so the
# short forms only count after on/by/this/next.
LONG_WEEKDAYS = _either(w for w in WEEKDAYS if w not in ("sat", "sun"))


# --- What each pattern means, turned into a real date --------------------

def _upcoming(today, month, day):
    """The next time this day/month happens (today counts)."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:  # e.g. 31 Feb
            continue
        if d >= today:
            return d
    return None


def _day_month(m, today):
    month = MONTHS[m["mon"]] if m["mon"].isalpha() else int(m["mon"])
    day = int(m["day"])
    if m["year"]:
        year = int(m["year"])
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return _upcoming(today, month, day)


def _ordinal(m, today):
    # "by the 30th" -> the 30th of this month, or next month if it's passed.
    day = int(m["day"])
    year, month = today.year, today.month
    for _ in range(3):
        try:
            d = date(year, month, day)
            if d >= today:
                return d
        except ValueError:
            pass
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def _today(m, today):
    return today


def _tomorrow(m, today):
    return today + timedelta(days=2 if m["after"] else 1)


def _in_days(m, today):
    n = m["n"]
    n = int(n) if n.isdigit() else NUMBER_WORDS[n]
    if m["unit"].startswith("week"):
        n *= 7
    return today + timedelta(days=n)


def _next_week(m, today):
    return today + timedelta(days=7)


def _weekday(m, today):
    ahead = (WEEKDAYS[m["wd"]] - today.weekday()) % 7
    if ahead == 0 and m["mod"] == "next":
        ahead = 7  # "next friday" said on a Friday means a week later
    return today + timedelta(days=ahead)


PATTERNS = [
    # 25 oct, 25th october, 25 oct 2026
    (r"\b" + DAY + r"\s*(?:of\s+)?" + MONTH + YEAR + r"(?!\d)", _day_month),
    # oct 25, october 25th, oct 25 2026
    (r"\b" + MONTH + r"\s*" + DAY + YEAR + r"(?![a-z0-9])", _day_month),
    # 25/10 or 25/10/2026
    (r"\b(?P<day>\d{1,2})/(?P<mon>\d{1,2})(?:/(?P<year>\d{4}|\d{2}))?(?![\d/])", _day_month),
    # 25-10-2026 or 25.10.26 (only with a year, so "pages 10-12" or "5.2" stay normal)
    (r"\b(?P<day>\d{1,2})([-.])(?P<mon>\d{1,2})\2(?P<year>\d{4}|\d{2})(?![\d.-])", _day_month),
    # by the 30th, on 5th
    (r"\b(?:on|by|before|till|until)\s+(?:the\s+)?(?P<day>\d{1,2})(?:st|nd|rd|th)(?![a-z0-9])", _ordinal),
    (r"\b(?:today|tonight|tdy)\b", _today),
    (r"\b(?P<after>day\s+after\s+)?(?:tomorrow|tomorow|tommorow|tommorrow|tmrw|tmrow|tmr|2morrow|2moro)\b", _tomorrow),
    # in 3 days, after a week
    (r"\b(?:in|after|within)\s+(?P<n>\d{1,3}|" + _either(NUMBER_WORDS) + r")\s+(?P<unit>days?|weeks?)\b", _in_days),
    (r"\bnext\s+week\b", _next_week),
    # friday, this friday, next monday
    (r"\b(?:(?P<mod>this|next|coming|upcoming)\s+)?(?P<wd>" + LONG_WEEKDAYS + r")(?![a-z])", _weekday),
    # on sat, next sun
    (r"\b(?:(?P<mod>this|next|coming|upcoming)|on|by)\s+(?P<wd>sat|sun)(?![a-z])", _weekday),
]
PATTERNS = [(re.compile(p, re.IGNORECASE), fn) for p, fn in PATTERNS]

# Little words that belong to the date, not the task: "submit lab BY friday"
LEFTOVER = re.compile(r"(?:\b(?:on|by|before|due|till|until|for)|@)\s*$", re.IGNORECASE)


def extract_date(text, today):
    """Return (task_text_without_the_date, due_date_or_None)."""
    found = []  # (start, end, date)
    for regex, to_date in PATTERNS:
        for m in regex.finditer(text):
            # Match the regex on lowercase names (MONTHS/WEEKDAYS keys are lowercase).
            groups = {k: (v.lower() if v else v) for k, v in m.groupdict().items()}
            d = to_date(groups, today)
            if d:
                found.append((m.start(), m.end(), d))
    if not found:
        return clean(text), None

    # Deadlines are usually said last, so take the date that ends last.
    # If two end at the same spot, take the longer one ("day after tomorrow").
    start, end, due = max(found, key=lambda f: (f[1], -f[0]))

    before = text[:start]
    while LEFTOVER.search(before):
        before = LEFTOVER.sub("", before)
    return clean(before + " " + text[end:]), due


# --- Times ----------------------------------------------------------------

# "at 5pm", "by 11:59 pm", "5.30pm" (needs am/pm) ...
TIME_AMPM = re.compile(r"(?:\b(?:at|by|before|till|until)\s+)?\b(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>[ap])\.?m\b\.?", re.IGNORECASE)
# ... "17:30", "at 10:30" (24-hour, needs a colon so "chapter 5.30" stays normal) ...
TIME_24 = re.compile(r"(?:\b(?:at|by|before|till|until)\s+)?\b(?P<h>\d{1,2}):(?P<m>\d{2})(?![\d:])", re.IGNORECASE)
# ... "at noon", "midnight"
TIME_WORD = re.compile(r"(?:\b(?:at|by|before|till|until)\s+)?\b(?P<word>noon|midnight)\b", re.IGNORECASE)


def _to_time(m):
    """Turn a time match into a datetime.time, or None if it isn't a real time (like 25:00)."""
    groups = m.groupdict()
    if groups.get("word"):
        return time(12, 0) if groups["word"].lower() == "noon" else time(0, 0)
    hour, minute = int(groups["h"]), int(groups["m"] or 0)
    if minute > 59:
        return None
    if groups.get("ampm"):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if groups["ampm"].lower() == "p" else 0)  # 12am = 0:00, 12pm = 12:00
    elif hour > 23:
        return None
    return time(hour, minute)


def extract_time(text):
    """Return (text_without_the_time, time_or_None)."""
    found = []
    for regex in (TIME_AMPM, TIME_24, TIME_WORD):
        for m in regex.finditer(text):
            t = _to_time(m)
            if t:
                found.append((m.start(), m.end(), t))
    if not found:
        return text, None
    start, end, t = max(found, key=lambda f: (f[1], -f[0]))
    return text[:start] + " " + text[end:], t


def extract_when(text, now):
    """Find a date AND a time. now = the current time in India (no timezone).
    Returns (task, due_date_or_None, time_or_None). A time with no date gives due None:
    the caller decides which day that means."""
    text, due_time = extract_time(text)
    task, due = extract_date(text, now.date())
    return task, due, due_time


def day_for_time(due_time, now):
    """A time without a date ("call mom 5pm") means today, or tomorrow if it already passed."""
    return now.date() if due_time > now.time() else now.date() + timedelta(days=1)


def clean(text):
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,.;:!?-–—")
