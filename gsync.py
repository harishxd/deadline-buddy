"""Two-way sync between the owner's tasks and a "Deadline Buddy" calendar in their Google account.

Only the owner (OWNER_CHAT_ID) uses this. Everyone else keeps the read-only .ics link.

    bot -> Google: push_task() / delete_event() right after a task changes
    Google -> bot: pull() at the start of each of the owner's messages

Plain `requests` calls, same style as the Telegram code. Nothing here ever raises
because of Google being down, and tokens are never printed.
"""
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo
import requests

INDIA = ZoneInfo("Asia/Kolkata")
# Lets the bot create its own calendars and manage events in them, and nothing else.
SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"


def explain(r):
    """Google's error text for the logs. It never contains tokens."""
    try:
        err = r.json().get("error")
        return err.get("message") if isinstance(err, dict) else err
    except ValueError:
        return r.text[:200]


# --- Signing in (OAuth) ---------------------------------------------------

def sign_in_link(state, redirect_uri):
    """The Google page where the owner presses Allow. `state` ties the answer to this chat."""
    return AUTH_URL + "?" + urlencode({
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",  # = also give us a refresh token
        "prompt": "consent",       # = always give a new refresh token, even on a second sign-in
        "state": state,
    })


def exchange_code(code, redirect_uri):
    """Swap the one-time code from Google for tokens. Returns the token dict or None."""
    try:
        r = requests.post(TOKEN_URL, timeout=10, data={
            "code": code,
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    except Exception as e:
        print("Google token exchange error:", type(e).__name__)
        return None
    if not r.ok:
        print("Google token exchange failed:", r.status_code, explain(r))
        return None
    return r.json()


def revoke(refresh_token):
    """Tell Google to forget our access (used by /disconnect). Best effort."""
    try:
        requests.post(REVOKE_URL, params={"token": refresh_token}, timeout=10)
    except Exception as e:
        print("Google revoke error:", type(e).__name__)


# --- Calling the Calendar API ---------------------------------------------

def load_account(cur, chat_id):
    """The owner's saved Google connection as a dict, or None if not connected."""
    cur.execute("SELECT refresh_token, access_token, access_expires, calendar_id, sync_token "
                "FROM google_accounts WHERE chat_id = %s AND refresh_token IS NOT NULL", (chat_id,))
    row = cur.fetchone()
    if row is None:
        return None
    keys = ("refresh_token", "access_token", "access_expires", "calendar_id", "sync_token")
    return {"chat_id": chat_id, **dict(zip(keys, row))}


def access_token(cur, acct):
    """An access token works for about an hour. When it runs out, use the refresh token to get a new one."""
    if acct["access_token"] and acct["access_expires"] and acct["access_expires"] > datetime.now(timezone.utc):
        return acct["access_token"]
    try:
        r = requests.post(TOKEN_URL, timeout=10, data={
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "refresh_token": acct["refresh_token"],
            "grant_type": "refresh_token",
        })
    except Exception as e:
        print("Google token refresh error:", type(e).__name__)
        return None
    if not r.ok:
        print("Google token refresh failed:", r.status_code, explain(r))
        if explain(r) == "invalid_grant":
            # Access was removed in the Google account (or it expired). Stop until /connect again.
            cur.execute("UPDATE google_accounts SET refresh_token = NULL, access_token = NULL WHERE chat_id = %s",
                        (acct["chat_id"],))
            acct["broken"] = True
        return None
    data = r.json()
    acct["access_token"] = data["access_token"]
    acct["access_expires"] = datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600) - 60)
    cur.execute("UPDATE google_accounts SET access_token = %s, access_expires = %s WHERE chat_id = %s",
                (acct["access_token"], acct["access_expires"], acct["chat_id"]))
    return acct["access_token"]


def google(cur, acct, method, path, **kwargs):
    """Call the Calendar API. Returns the response, or None if we couldn't reach it."""
    if acct.get("broken"):
        return None
    token = access_token(cur, acct)
    if not token:
        return None
    try:
        return requests.request(method, API + path, timeout=10,
                                headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except Exception as e:
        print("Google API error:", method, type(e).__name__)
        return None


def calendar_path(acct):
    # Calendar ids look like "abc123@group.calendar.google.com", so they need escaping in a URL.
    return "/calendars/" + quote(acct["calendar_id"], safe="")


def create_calendar(cur, acct):
    r = google(cur, acct, "POST", "/calendars", json={
        "summary": "Deadline Buddy",
        "description": "Tasks from the Deadline Buddy Telegram bot",
        "timeZone": "Asia/Kolkata",
    })
    if r is None or not r.ok:
        print("Couldn't create calendar:", r.status_code if r is not None else "-", explain(r) if r is not None else "")
        return None
    return r.json()["id"]


# --- Tasks <-> events -----------------------------------------------------

def event_body(task, due, due_time):
    """A task as a Google Calendar event."""
    body = {"summary": task, "description": "From Deadline Buddy on Telegram", "status": "confirmed"}
    if due_time is None:
        # All-day. Like in .ics, the end date is the day after.
        body["start"] = {"date": due.isoformat()}
        body["end"] = {"date": (due + timedelta(days=1)).isoformat()}
        body["transparency"] = "transparent"  # don't show you as busy all day
        body["reminders"] = {"useDefault": False}
    else:
        start = datetime.combine(due, due_time)
        end = start + timedelta(hours=1)
        body["start"] = {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": "Asia/Kolkata"}
        # A phone alert 30 minutes before. (A new calendar has no default alerts.)
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}
    return body


def event_when(event):
    """(due_date, due_time_or_None) from a Google event, in India time. None if it has no start."""
    start = event.get("start", {})
    if "date" in start:
        return date.fromisoformat(start["date"]), None
    if "dateTime" in start:
        when = datetime.fromisoformat(start["dateTime"]).astimezone(INDIA)
        return when.date(), when.time().replace(second=0, microsecond=0)
    return None


def push_task(cur, acct, task_id):
    """Make Google match one task: create, update, or (if it lost its date) delete its event."""
    cur.execute("SELECT task, due, due_time, gcal_event_id FROM tasks WHERE id = %s AND chat_id = %s",
                (task_id, acct["chat_id"]))
    row = cur.fetchone()
    if row is None:
        return
    task, due, due_time, event_id = row
    events = calendar_path(acct) + "/events"

    if due is None:
        # No date = doesn't belong in the calendar.
        if event_id:
            delete_event(cur, acct, event_id)
            cur.execute("UPDATE tasks SET gcal_event_id = NULL WHERE id = %s", (task_id,))
        return

    body = event_body(task, due, due_time)
    if event_id:
        r = google(cur, acct, "PUT", f"{events}/{quote(event_id)}", json=body)
        if r is not None and r.ok:
            return
        if r is None or r.status_code != 404:
            print("Couldn't update event:", r.status_code if r is not None else "-", explain(r) if r is not None else "")
            return
        # 404: someone deleted it in Google meanwhile. Make a fresh one below.

    # We choose the event id ourselves ("dbtask57"), so we always know which task it belongs to.
    # (Google only allows the letters a-v and digits in ids. "dbtask" fits.)
    new_id = f"dbtask{task_id}"
    r = google(cur, acct, "POST", events, json={**body, "id": new_id})
    if r is not None and r.status_code == 409:
        # That id was used before (a deleted event keeps its id), so update it back to life instead.
        r = google(cur, acct, "PUT", f"{events}/{new_id}", json=body)
    if r is not None and r.ok:
        cur.execute("UPDATE tasks SET gcal_event_id = %s WHERE id = %s", (new_id, task_id))
    else:
        print("Couldn't create event:", r.status_code if r is not None else "-", explain(r) if r is not None else "")


def delete_event(cur, acct, event_id):
    r = google(cur, acct, "DELETE", f"{calendar_path(acct)}/events/{quote(event_id)}")
    # 404/410 = already gone, which is what we wanted anyway.
    if r is not None and r.status_code not in (200, 204, 404, 410):
        print("Couldn't delete event:", r.status_code, explain(r))


def push_unsynced(cur, acct, everything=False):
    """Push tasks that never made it to Google (or, with everything=True, all dated tasks)."""
    sql = "SELECT id FROM tasks WHERE chat_id = %s AND due IS NOT NULL"
    if not everything:
        sql += " AND gcal_event_id IS NULL"
    cur.execute(sql, (acct["chat_id"],))
    for (task_id,) in cur.fetchall():
        push_task(cur, acct, task_id)


def pull(cur, acct):
    """Bring changes made in Google Calendar into the bot.

    Google gives us a "sync token" after each pull. Next time we hand it back and get only
    what changed since then (including deleted events), instead of every event again."""
    params = {"showDeleted": "true", "maxResults": 250}
    if acct["sync_token"]:
        params["syncToken"] = acct["sync_token"]
    changed = []
    while True:
        r = google(cur, acct, "GET", calendar_path(acct) + "/events", params=params)
        if r is None:
            return
        if r.status_code == 410:
            # The sync token is too old. Start again with a full list.
            acct["sync_token"] = None
            return pull(cur, acct)
        if not r.ok:
            print("Couldn't read events:", r.status_code, explain(r))
            return
        data = r.json()
        changed += data.get("items", [])
        if "nextPageToken" in data:
            params["pageToken"] = data["nextPageToken"]
            continue
        new_token = data.get("nextSyncToken")
        break

    for event in changed:
        apply_event(cur, acct, event)
    acct["sync_token"] = new_token
    cur.execute("UPDATE google_accounts SET sync_token = %s WHERE chat_id = %s", (new_token, acct["chat_id"]))


def apply_event(cur, acct, event):
    """One event that changed in Google -> the matching change to the owner's tasks."""
    chat_id, event_id = acct["chat_id"], event["id"]
    cur.execute("SELECT id, task, due, due_time FROM tasks WHERE chat_id = %s AND gcal_event_id = %s",
                (chat_id, event_id))
    row = cur.fetchone()

    if event.get("status") == "cancelled":
        if row:
            cur.execute("DELETE FROM tasks WHERE id = %s", (row[0],))  # deleted in Google = done
        return
    if event.get("recurrence") or event.get("recurringEventId"):
        return  # repeating events aren't tasks
    when = event_when(event)
    if when is None:
        return
    due, due_time = when
    task = (event.get("summary") or "").strip() or "(no title)"

    if row:
        if (row[1], row[2], row[3]) != (task, due, due_time):
            cur.execute("UPDATE tasks SET task = %s, due = %s, due_time = %s WHERE id = %s",
                        (task, due, due_time, row[0]))
    elif event_id.startswith("dbtask"):
        # The bot made this event but the task is gone (the delete must have failed earlier). Tidy up.
        delete_event(cur, acct, event_id)
    else:
        # Made directly in Google Calendar: it becomes a task.
        cur.execute("INSERT INTO tasks (chat_id, task, due, due_time, gcal_event_id) VALUES (%s, %s, %s, %s, %s)",
                    (chat_id, task, due, due_time, event_id))


def finish_connect(cur, chat_id, tokens):
    """After Google says yes: save the tokens, make sure our calendar exists, and sync everything.
    Returns how many tasks are now in Google Calendar, or None if something failed."""
    cur.execute("UPDATE google_accounts SET refresh_token = %s, access_token = %s, access_expires = %s, "
                "sync_token = NULL WHERE chat_id = %s",
                (tokens["refresh_token"], tokens["access_token"],
                 datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600) - 60), chat_id))
    acct = load_account(cur, chat_id)

    # Reuse the calendar from an earlier connection if it still exists.
    if acct["calendar_id"]:
        r = google(cur, acct, "GET", calendar_path(acct))
        if r is None or not r.ok:
            acct["calendar_id"] = None
    if not acct["calendar_id"]:
        calendar_id = create_calendar(cur, acct)
        if not calendar_id:
            return None
        acct["calendar_id"] = calendar_id
        cur.execute("UPDATE google_accounts SET calendar_id = %s WHERE chat_id = %s", (calendar_id, chat_id))
        # Event ids from an older calendar mean nothing in the new one.
        cur.execute("UPDATE tasks SET gcal_event_id = NULL WHERE chat_id = %s", (chat_id,))

    pull(cur, acct)
    push_unsynced(cur, acct, everything=True)
    cur.execute("SELECT COUNT(*) FROM tasks WHERE chat_id = %s AND gcal_event_id IS NOT NULL", (chat_id,))
    return cur.fetchone()[0]
