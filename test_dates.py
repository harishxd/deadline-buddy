"""Tests for dates.py. Run with:  python test_dates.py"""
from datetime import date, datetime, time
from dates import extract_date, extract_when, day_for_time

TODAY = date(2026, 10, 21)  # a Wednesday. Fixed, so the tests give the same answer every day.

CASES = [
    # (what the user types,              expected task,           expected due date)
    ("chem record friday",               "chem record",           date(2026, 10, 23)),
    ("physics test 25 oct",              "physics test",          date(2026, 10, 25)),
    ("submit lab tomorrow",              "submit lab",            date(2026, 10, 22)),
    ("pay fees next monday",             "pay fees",              date(2026, 10, 26)),
    ("maths assignment due on 3rd november", "maths assignment",  date(2026, 11, 3)),
    ("project review on 5/11",           "project review",        date(2026, 11, 5)),
    ("english essay by the 30th",        "english essay",         date(2026, 10, 30)),
    ("call mom today",                   "call mom",              date(2026, 10, 21)),
    ("bio quiz day after tomorrow",      "bio quiz",              date(2026, 10, 23)),
    ("return library book in 3 days",    "return library book",   date(2026, 10, 24)),
    ("hackathon registration in a week", "hackathon registration", date(2026, 10, 28)),
    ("dbms lab record @ 2 nov",          "dbms lab record",       date(2026, 11, 2)),
    ("Workshop Oct 30th",                "Workshop",              date(2026, 10, 30)),
    ("seminar this wednesday",           "seminar",               date(2026, 10, 21)),
    ("gym on sat",                       "gym",                   date(2026, 10, 24)),
    ("submit tmrw",                      "submit",                date(2026, 10, 22)),
    ("lab viva next week",               "lab viva",              date(2026, 10, 28)),
    ("pay hostel fees 1/11/2026",        "pay hostel fees",       date(2026, 11, 1)),
    ("semester exam 15 jan 2027",        "semester exam",         date(2027, 1, 15)),
    ("pages 10-12 by friday",            "pages 10-12",           date(2026, 10, 23)),
    ("Physics Test on 25th Oct, 2026",   "Physics Test",          date(2026, 10, 25)),
    ("meeting 10:30 tomorrow",           "meeting 10:30",         date(2026, 10, 22)),
    ("Tech shodha at 30th oct",          "Tech shodha",           date(2026, 10, 30)),  # real message that broke
    ("fest on the 25th oct",             "fest",                  date(2026, 10, 25)),
    ("C programming module 2 test on Friday", "C programming module 2 test", date(2026, 10, 23)),
    # A date that already passed this year means next year ("prefer future").
    ("physics test 20 oct",              "physics test",          date(2027, 10, 20)),
    # Normal words and numbers that must NOT become dates:
    ("chapter 5 notes",                  "chapter 5 notes",       None),
    ("lab 3",                            "lab 3",                 None),
    ("may i borrow your notes",          "may i borrow your notes", None),
    ("read 2 chapters of physics",       "read 2 chapters of physics", None),
    ("buy 10 pens",                      "buy 10 pens",           None),
    ("march past practice",              "march past practice",   None),
    ("chapter 5.2 notes",                "chapter 5.2 notes",     None),
    ("SAT prep",                         "SAT prep",              None),
    ("tom and jerry notes",              "tom and jerry notes",   None),
    ("fees 31 feb",                      "fees 31 feb",           None),  # not a real date
]


def test_extract_date():
    failed = 0
    for text, want_task, want_due in CASES:
        got_task, got_due = extract_date(text, TODAY)
        if (got_task, got_due) != (want_task, want_due):
            failed += 1
            print(f"FAIL  {text!r}\n      got  {got_task!r}, {got_due}\n      want {want_task!r}, {want_due}")
    assert failed == 0, f"{failed} of {len(CASES)} cases failed"


NOW = datetime(2026, 10, 21, 14, 0)  # Wednesday, 2:00 PM in India

TIME_CASES = [
    # (what the user types,           task,              date,               time)
    ("call mom tomorrow 5pm",         "call mom",        date(2026, 10, 22), time(17, 0)),
    ("physics test 25 oct at 10:30",  "physics test",    date(2026, 10, 25), time(10, 30)),
    ("meeting 10:30 tomorrow",        "meeting",         date(2026, 10, 22), time(10, 30)),
    ("submit friday by 11:59pm",      "submit",          date(2026, 10, 23), time(23, 59)),
    ("lunch at noon friday",          "lunch",           date(2026, 10, 23), time(12, 0)),
    ("12am exam tomorrow",            "exam",            date(2026, 10, 22), time(0, 0)),
    ("viva 9.30 am on 3rd nov",       "viva",            date(2026, 11, 3),  time(9, 30)),
    ("dance class 6 p.m. monday",     "dance class",     date(2026, 10, 26), time(18, 0)),
    # A time with no date: due is None here, the bot picks the day with day_for_time().
    ("call mom 5 pm",                 "call mom",        None,               time(17, 0)),
    # Must NOT become times:
    ("chapter 5.30 notes",            "chapter 5.30 notes", None,            None),
    ("ratio 16:9 problems",           "ratio 16:9 problems", None,           None),
    ("submit at 25:00",               "submit at 25:00", None,               None),
    ("exam 13pm",                     "exam 13pm",       None,               None),
    ("buy 5 apples",                  "buy 5 apples",    None,               None),
]


def test_extract_when():
    failed = 0
    for text, want_task, want_due, want_time in TIME_CASES:
        got = extract_when(text, NOW)
        if got != (want_task, want_due, want_time):
            failed += 1
            print(f"FAIL  {text!r}\n      got  {got}\n      want {(want_task, want_due, want_time)}")
    assert failed == 0, f"{failed} of {len(TIME_CASES)} time cases failed"


def test_day_for_time():
    assert day_for_time(time(17, 0), NOW) == date(2026, 10, 21)  # 5 PM hasn't happened yet: today
    assert day_for_time(time(6, 0), NOW) == date(2026, 10, 22)   # 6 AM already passed: tomorrow


if __name__ == "__main__":
    test_extract_date()
    test_extract_when()
    test_day_for_time()
    print(f"All {len(CASES)} date tests and {len(TIME_CASES)} time tests passed ✅")
