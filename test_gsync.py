"""Tests for the parts of gsync.py that don't need Google. Run with:  python test_gsync.py"""
from datetime import date, time
from urllib.parse import urlparse, parse_qs
from gsync import event_body, event_when, sign_in_link, SCOPE


def test_all_day_event():
    body = event_body("chem record", date(2026, 10, 23), None)
    assert body["start"] == {"date": "2026-10-23"}
    assert body["end"] == {"date": "2026-10-24"}  # end = the day after
    assert body["summary"] == "chem record"


def test_timed_event():
    body = event_body("call mom", date(2026, 10, 23), time(23, 30))
    assert body["start"] == {"dateTime": "2026-10-23T23:30:00", "timeZone": "Asia/Kolkata"}
    assert body["end"] == {"dateTime": "2026-10-24T00:30:00", "timeZone": "Asia/Kolkata"}  # crosses midnight
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


def test_reading_events_back():
    # Round trip: what we send is what we read back.
    assert event_when(event_body("x", date(2026, 10, 23), None)) == (date(2026, 10, 23), None)
    assert event_when({"start": {"dateTime": "2026-10-23T17:00:00+05:30"}}) == (date(2026, 10, 23), time(17, 0))
    # An event made in Google in another time zone is shown in India time: 11:30 UTC = 5:00 PM IST
    assert event_when({"start": {"dateTime": "2026-10-23T11:30:00Z"}}) == (date(2026, 10, 23), time(17, 0))
    # 8 PM in London (UTC+1 in October) = 00:30 next day in India
    assert event_when({"start": {"dateTime": "2026-10-23T20:00:00+01:00"}}) == (date(2026, 10, 24), time(0, 30))
    assert event_when({}) is None


def test_sign_in_link():
    query = parse_qs(urlparse(sign_in_link("abc123", "https://example.com/oauth/callback")).query)
    assert query["scope"] == [SCOPE]
    assert query["state"] == ["abc123"]
    assert query["access_type"] == ["offline"]  # needed to get a refresh token
    assert query["redirect_uri"] == ["https://example.com/oauth/callback"]


if __name__ == "__main__":
    test_all_day_event()
    test_timed_event()
    test_reading_events_back()
    test_sign_in_link()
    print("Google sync tests passed ✅")
