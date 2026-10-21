"""Tests for dates.py. Run with:  python test_dates.py"""
from datetime import date
from dates import extract_date

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


if __name__ == "__main__":
    test_extract_date()
    print(f"All {len(CASES)} date tests passed ✅")
