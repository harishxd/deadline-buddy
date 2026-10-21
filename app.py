import os
import re
import hmac
import secrets
import traceback
import requests
import psycopg
import dateparser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, g, jsonify, Response
from dotenv import load_dotenv
from dates import extract_when, day_for_time, clean
from icsfeed import make_ics
import gsync

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
URL = f"https://api.telegram.org/bot{TOKEN}/"
# A password only Telegram knows (we give it to Telegram with setWebhook).
# Every real update carries it, so fake updates from anyone else are rejected.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
# Where the bot lives on the internet. Used to build calendar links.
BASE_URL = os.getenv("BASE_URL", "https://deadline-buddy-one.vercel.app")
# Only this chat gets two-way Google Calendar sync (/connect). Everyone else uses the .ics link.
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
# Where Google sends you back after you press Allow. Must match the Google Cloud settings exactly.
OAUTH_REDIRECT = os.getenv("OAUTH_REDIRECT_URI", f"{BASE_URL}/oauth/callback")

# Local testing: with DRY_RUN=1 we don't call Telegram at all. The calls we
# *would* make are returned from /webhook so fake_telegram.py can print them.
DRY_RUN = os.getenv("DRY_RUN") == "1"

app = Flask(__name__)

# The big grey buttons under the typing box. They just send their label as a
# normal text message, so handle_message() checks for these exact strings.
BTN_LIST = "📋 My tasks"
BTN_ADD = "➕ Add"
BTN_CAL = "📅 Calendar"  # no longer on the keyboard (use /calendar), still understood from old keyboards
BTN_HELP = "❓ Help"

MAIN_KEYBOARD = {
    "keyboard": [[BTN_LIST, BTN_ADD, BTN_HELP]],  # one row, so it takes less space
    "resize_keyboard": True,
    "is_persistent": True,
}

HELP_TEXT = (
    "Hi! I'm Deadline Buddy ⏳\n\n"
    "I remember your tasks so you don't have to.\n\n"
    "Just type a task, like:\n"
    "• chem record friday\n"
    "• physics test 25 oct\n"
    "• submit lab tomorrow\n"
    "• call mom friday 5pm\n\n"
    "Buttons below:\n"
    f"{BTN_LIST} – see your tasks, tap ✅ to finish one or ✏️ to change it\n\n"
    "Commands also work:\n"
    "/add <task> @ <date> – save a task\n"
    "/list – see your tasks\n"
    "/edit <number> – change a task\n"
    "/done <number> – remove a task\n"
    "/doneall – clear all tasks\n"
    "/calendar – see your deadlines in Google Calendar\n\n"
    "☀️ Every morning I'll remind you what's overdue, due today and due tomorrow."
)


def safe(text):
    """Hide secrets before anything gets printed to the logs."""
    text = str(text)
    for secret in (TOKEN, os.getenv("DATABASE_URL"), os.getenv("CRON_SECRET"), WEBHOOK_SECRET,
                   os.getenv("GOOGLE_CLIENT_SECRET")):
        if secret:
            text = text.replace(secret, "***")
    return text


def tg(method, **params):
    """Call one Telegram Bot API method. Never raises, never logs the token."""
    if DRY_RUN:
        g.setdefault("sent", []).append({"method": method, **params})
        return None
    try:
        r = requests.post(URL + method, json=params, timeout=10)
        if not r.ok:
            # "description" is Telegram's error text. It never contains the token.
            print("Telegram", method, "failed:", r.status_code, r.json().get("description"))
        return r
    except Exception as e:
        # Don't print e itself: requests errors include the URL, and the URL has the token!
        print("Telegram", method, "error:", type(e).__name__)
        return None


_db_ready = False


def connect():
    """Open a DB connection. The first time per server process, make sure the tables exist."""
    global _db_ready
    conn = psycopg.connect(os.getenv("DATABASE_URL"), connect_timeout=10)
    if not _db_ready:
        try:
            cur = conn.cursor()
            # Every statement is "IF NOT EXISTS", so running it again is harmless
            # and never touches existing rows.
            cur.execute("CREATE TABLE IF NOT EXISTS tasks (id SERIAL PRIMARY KEY, chat_id BIGINT, task TEXT)")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due DATE")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due_time TIME")
            cur.execute("CREATE INDEX IF NOT EXISTS tasks_chat_id_idx ON tasks (chat_id)")
            cur.execute("CREATE TABLE IF NOT EXISTS users (chat_id BIGINT PRIMARY KEY, first_name TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cal_token TEXT")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_cal_token_idx ON users (cal_token)")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS editing_task_id INTEGER")
            # Google sync (owner only): which event belongs to which task, and the saved sign-in.
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS gcal_event_id TEXT")
            cur.execute("CREATE TABLE IF NOT EXISTS google_accounts (chat_id BIGINT PRIMARY KEY, "
                        "refresh_token TEXT, access_token TEXT, access_expires TIMESTAMPTZ, calendar_id TEXT, "
                        "sync_token TEXT, oauth_state TEXT, oauth_state_at TIMESTAMPTZ)")
            conn.commit()
        except Exception:
            conn.close()
            raise
        _db_ready = True
    return conn


def is_owner(chat_id):
    return bool(OWNER_CHAT_ID) and str(chat_id) == OWNER_CHAT_ID.strip()


def google_safely(cur, what, fn, *args):
    """Run a Google sync step without ever breaking the bot.

    conn.transaction() inside a transaction makes a "savepoint": if this step crashes,
    only its own database changes are undone, and the task change itself still saves."""
    try:
        with cur.connection.transaction():
            fn(cur, *args)
    except Exception:
        print(f"Google {what} failed:", safe(traceback.format_exc()))


def start_sync(cur, chat_id):
    """For the owner, if connected: remember the Google account for this request
    and first pull any changes made in Google Calendar."""
    g.gacct = None
    if not is_owner(chat_id):
        return
    acct = gsync.load_account(cur, chat_id)
    if acct:
        g.gacct = acct
        google_safely(cur, "pull", gsync.pull, acct)


def sync_task(cur, task_id):
    """A task was added or changed: update Google too (owner only)."""
    if g.get("gacct"):
        google_safely(cur, "push", gsync.push_task, g.gacct, task_id)


def sync_deleted(cur, event_ids):
    """Tasks were deleted: delete their Google events too (owner only)."""
    if g.get("gacct"):
        for event_id in event_ids:
            if event_id:
                google_safely(cur, "delete", gsync.delete_event, g.gacct, event_id)


def save_user(cur, chat_id, first_name):
    """Add/update the user. Returns the id of the task they're editing (or None)."""
    # "Upsert": add the user the first time, update their name after that.
    cur.execute(
        "INSERT INTO users (chat_id, first_name) VALUES (%s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET first_name = EXCLUDED.first_name "
        "RETURNING editing_task_id",
        (chat_id, first_name),
    )
    return cur.fetchone()[0]


def india_now():
    """The time in India right now, without timezone info (easier to compare)."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def india_today():
    return india_now().date()


# Same order everywhere, so "/done 3" means the 3rd task in /list.
# Inside one day, all-day tasks come first, then timed ones by time.
TASK_ORDER = "ORDER BY due NULLS LAST, due_time NULLS FIRST, id"


def nice_date(d):
    if d.year == india_today().year:
        return d.strftime('%a, %d %b')
    return d.strftime('%a, %d %b %Y')


def nice_time(t):
    return t.strftime("%I:%M %p").lstrip("0")  # 17:00 -> "5:00 PM"


def nice_when(due, due_time):
    text = nice_date(due)
    if due_time:
        text += f", {nice_time(due_time)}"
    return text


GREETINGS = {"hi", "hello", "hey", "yo", "hola", "namaste", "thanks", "thank you",
             "thx", "ty", "ok", "okay", "k", "cool", "nice", "good morning", "gm", "good night", "gn", "bye"}


def squash(text):
    """Squash repeated letters, so "hellooooo" and "hiii" look like "helo" and "hi"."""
    return re.sub(r"(.)\1+", r"\1", text)


SQUASHED_GREETINGS = {squash(g) for g in GREETINGS}


def is_greeting(text):
    return squash(text.lower().strip("!.? ")) in SQUASHED_GREETINGS


def parse_task(text, now):
    """Split one line into (task, due, due_time). Returns an error message (str) if it can't."""
    if " @" in text:
        # Old style: "task @ date". Everything after @ is the date.
        # (Needs a space before @, so emails like prof@college.edu are left alone.)
        task, _, date_text = text.partition(" @")
        task = clean(task)
        _, due, due_time = extract_when(date_text, now)
        if due is None and due_time is None and date_text.strip():
            # Our patterns didn't know it. Let dateparser have a go ("in 2 months" etc).
            parsed = dateparser.parse(date_text, settings={"PREFER_DATES_FROM": "future", "DATE_ORDER": "DMY", "RELATIVE_BASE": now})
            if parsed is None:
                return "I couldn't understand that date 🤔 Try: physics test 25 oct"
            due = parsed.date()
    else:
        task, due, due_time = extract_when(text, now)

    if not task:
        return "What's the task? Type it like this:\nchem record friday"
    if due_time and not due:
        due = day_for_time(due_time, now)  # "call mom 5pm" = today (or tomorrow if 5pm passed)
    return task, due, due_time


def insert_task(cur, chat_id, task, due, due_time):
    # RETURNING id gives us the new row's id, so the Undo button knows what to delete.
    cur.execute("INSERT INTO tasks (chat_id, task, due, due_time) VALUES (%s, %s, %s, %s) RETURNING id",
                (chat_id, task, due, due_time))
    task_id = cur.fetchone()[0]
    sync_task(cur, task_id)
    return task_id


def add_task(cur, chat_id, text):
    """Save a task typed as normal text, e.g. "chem record friday"."""
    parsed = parse_task(text, india_now())
    if isinstance(parsed, str):
        return parsed  # an error message
    task, due, due_time = parsed
    task_id = insert_task(cur, chat_id, task, due, due_time)

    reply = f"Saved ✅ {task}"
    if due:
        reply += f"\n📅 {nice_when(due, due_time)}"
    return reply, [[{"text": "↩️ Undo", "callback_data": f"undo:{task_id}"},
                    {"text": "✏️ Edit", "callback_data": f"edit:{task_id}"}]]


def add_tasks(cur, chat_id, text):
    """A message with several lines = several tasks, one per line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return add_task(cur, chat_id, text)

    now = india_now()
    saved, skipped = [], []
    for line in lines[:20]:
        parsed = parse_task(line, now)
        if isinstance(parsed, str):
            skipped.append(line)
            continue
        saved.append((insert_task(cur, chat_id, *parsed), *parsed))
    if not saved:
        return "I couldn't find any tasks in that 🤔"

    reply = f"Saved ✅ {len(saved)} tasks:\n"
    for _, task, due, due_time in saved:
        reply += f"\n• {task}" + (f" — 📅 {nice_when(due, due_time)}" if due else "")
    if skipped:
        reply += "\n\nSkipped: " + ", ".join(skipped)
    if len(lines) > 20:
        reply += "\n\n(Only the first 20 lines were saved.)"
    # One button undoes them all. The new ids are in one range, and the chat_id
    # check in the DELETE makes sure only *your* tasks in that range are touched.
    ids = [row[0] for row in saved]
    return reply, [[{"text": f"↩️ Undo all {len(saved)}", "callback_data": f"undo:{min(ids)}-{max(ids)}"}]]


def start_edit(cur, chat_id, task_id):
    """Remember which task this user is editing, and tell them what to send."""
    cur.execute("SELECT task, due, due_time FROM tasks WHERE id = %s AND chat_id = %s", (task_id, chat_id))
    row = cur.fetchone()
    if row is None:
        return None
    task, due, due_time = row
    cur.execute("UPDATE users SET editing_task_id = %s WHERE chat_id = %s", (task_id, chat_id))
    current = f"{task} — 📅 {nice_when(due, due_time)}" if due else task
    return (
        f"✏️ Editing: {current}\n\n"
        "Send me the change:\n"
        "• a new day or time → monday 5pm\n"
        "• a new name → chem record part 2\n"
        "• both → chem record part 2 monday\n"
        "• no date → remove the date\n\n"
        "Or send /cancel to keep it as it is."
    )


def edit_task(cur, chat_id, task_id, text):
    """Apply the user's change to the task they're editing."""
    cur.execute("SELECT task, due, due_time FROM tasks WHERE id = %s AND chat_id = %s", (task_id, chat_id))
    row = cur.fetchone()
    if row is None:
        return "That task is already gone 🤔"
    old_task, old_due, old_time = row

    if text.lower().strip(" .!") == "no date":
        task, due, due_time = old_task, None, None
    else:
        now = india_now()
        task, due, due_time = extract_when(text, now)
        if not task:
            task = old_task  # they only sent a new day/time
        if due is None and due_time is None:
            due, due_time = old_due, old_time  # they only sent a new name
        elif due is None:
            due = old_due or day_for_time(due_time, now)  # only a new time: keep the day

    cur.execute("UPDATE tasks SET task = %s, due = %s, due_time = %s WHERE id = %s AND chat_id = %s",
                (task, due, due_time, task_id, chat_id))
    sync_task(cur, task_id)
    reply = f"Updated ✏️ {task}"
    if due:
        reply += f"\n📅 {nice_when(due, due_time)}"
    return reply, [[{"text": "✏️ Edit again", "callback_data": f"edit:{task_id}"}]]


def short(task, size=28):
    """Cut long task names so they fit on a button."""
    return task if len(task) <= size else task[:size - 1] + "…"


def list_view(cur, chat_id):
    """The task list text plus one ✅ button per task. Used by /list and after a ✅ tap."""
    cur.execute(f"SELECT id, task, due, due_time FROM tasks WHERE chat_id = %s {TASK_ORDER}", (chat_id,))
    rows = cur.fetchall()
    if not rows:
        return "No tasks left 🎉\nJust type one, like: chem record friday", None

    today = india_today()
    lines = ["📋 Your tasks:", ""]
    buttons = []
    for number, (task_id, task, due, due_time) in enumerate(rows, start=1):
        line = f"{number}. {task}"
        if due:
            line += f" — 📅 {nice_when(due, due_time)}"
            if due < today:
                line += " ⚠️"
        lines.append(line)
        if len(buttons) < 45:  # 2 buttons per row; Telegram allows 100 buttons per message
            # The buttons carry the real database id, not the list number.
            buttons.append([{"text": f"✅ {number}. {short(task)}", "callback_data": f"done:{task_id}"},
                            {"text": "✏️", "callback_data": f"edit:{task_id}"}])
    lines += ["", "Tap ✅ when you finish one, ✏️ to change it."]
    return "\n".join(lines)[:4000], buttons  # Telegram's limit is 4096 characters


def reminder_view(rows, today):
    """Morning reminder text and buttons for one user's (id, task, due, due_time) rows."""
    lines = ["Good morning! ☀️ Here's what's due:", ""]
    buttons = []
    for task_id, task, due, due_time in rows:
        if due < today:
            label = "⚠️ Overdue"
        elif due == today:
            label = "🔥 Today"
        else:
            label = "⏰ Tomorrow"
        if due_time:
            label += f" {nice_time(due_time)}"
        lines.append(f"{label}: {task}")
        buttons.append([{"text": f"✅ {short(task)}", "callback_data": f"rdone:{task_id}"}])
    return "\n".join(lines)[:4000], buttons[:50]


def calendar_message(cur, chat_id, reset):
    """Give the user their private calendar link (make a token the first time)."""
    # token_urlsafe(16) = 22 random letters/digits. Impossible to guess.
    new_token = secrets.token_urlsafe(16)
    if reset:
        cur.execute("UPDATE users SET cal_token = %s WHERE chat_id = %s", (new_token, chat_id))
    else:
        # Only sets a token if the user doesn't have one yet.
        cur.execute("UPDATE users SET cal_token = %s WHERE chat_id = %s AND cal_token IS NULL", (new_token, chat_id))
    cur.execute("SELECT cal_token FROM users WHERE chat_id = %s", (chat_id,))
    link = f"{BASE_URL}/cal/{cur.fetchone()[0]}.ics"

    first = "🔄 New link made. The old one doesn't work anymore.\n\n" if reset else ""
    return (
        f"{first}📅 Your private calendar link:\n{link}\n\n"
        "Add it to Google Calendar (on a computer):\n"
        "1. Open calendar.google.com\n"
        "2. On the left, next to \"Other calendars\", click +\n"
        "3. Choose \"From URL\", paste the link, click \"Add calendar\"\n\n"
        "Tasks with a date show up as all-day events. Google only checks for "
        "changes every few hours, so new tasks can take a while to appear.\n\n"
        "🔒 Anyone with this link can see your tasks. If you shared it by "
        "mistake, send /calendar reset to get a new one."
    ) + owner_sync_status(chat_id)


def owner_sync_status(chat_id):
    if not is_owner(chat_id):
        return ""
    if g.get("gacct"):
        return "\n\n🔁 Two-way Google sync is ON (the \"Deadline Buddy\" calendar). /sync to force a sync, /disconnect to stop."
    return "\n\n🔁 Two-way Google sync is off. Send /connect to turn it on."


def connect_message(cur, chat_id):
    """Owner only: send a Google sign-in button."""
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        return "Google isn't set up yet (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are missing)."
    # A random one-time code. Google hands it back to /oauth/callback, which proves the
    # sign-in was started here and not by someone else.
    state = secrets.token_urlsafe(24)
    cur.execute("INSERT INTO google_accounts (chat_id, oauth_state, oauth_state_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (chat_id) DO UPDATE SET oauth_state = EXCLUDED.oauth_state, oauth_state_at = now()",
                (chat_id, state))
    again = "You're already connected. Signing in again just refreshes it.\n\n" if g.get("gacct") else ""
    text = (
        f"{again}🔐 Tap the button, choose your Google account and press Allow.\n\n"
        "You'll see \"Google hasn't verified this app\". That's expected, it's your own app: "
        "tap Advanced → Go to Deadline Buddy.\n\n"
        "Make sure the calendar box is ticked. The button works for 15 minutes."
    )
    # A "url" button opens a link instead of sending callback_data.
    return text, [[{"text": "🔐 Sign in with Google", "url": gsync.sign_in_link(state, OAUTH_REDIRECT)}]]


def disconnect(cur, chat_id):
    acct = gsync.load_account(cur, chat_id)
    if acct is None:
        return "Google sync isn't on."
    gsync.revoke(acct["refresh_token"])
    cur.execute("DELETE FROM google_accounts WHERE chat_id = %s", (chat_id,))
    cur.execute("UPDATE tasks SET gcal_event_id = NULL WHERE chat_id = %s", (chat_id,))
    g.gacct = None
    return ("🔌 Google sync is off. Your tasks are still here.\n\n"
            "The \"Deadline Buddy\" calendar is still in Google Calendar. To remove it: "
            "Google Calendar → Settings → Deadline Buddy → Remove calendar.")


def handle_message(cur, chat_id, text, editing_id=None):
    if editing_id is not None:
        # They tapped ✏️ before, so this message is the change. Editing ends either way.
        cur.execute("UPDATE users SET editing_task_id = NULL WHERE chat_id = %s", (chat_id,))
        if text == "/cancel":
            return "Okay, nothing changed 👍"
        if not text.startswith("/") and text not in [BTN_LIST, BTN_ADD, BTN_CAL, BTN_HELP]:
            return edit_task(cur, chat_id, editing_id, text)
        # A command or button: stop editing and do that instead (below).

    if is_greeting(text):
        return "Hello buddy 👋 Just type a task, like: chem record friday"

    elif text in ["/start", "/help", BTN_HELP]:
        return HELP_TEXT

    elif text in ["/add", BTN_ADD]:
        return "Just type your task and when it's due 👇\n\nchem record friday\nphysics test 25 oct\nsubmit lab tomorrow"

    elif text.startswith("/add ") or text.startswith("/add\n"):
        return add_tasks(cur, chat_id, text[5:])

    elif text in ["/calendar", BTN_CAL]:
        return calendar_message(cur, chat_id, reset=False)

    elif text == "/calendar reset":
        return calendar_message(cur, chat_id, reset=True)

    elif text in ["/list", BTN_LIST]:
        return list_view(cur, chat_id)

    elif text == "/doneall":
        cur.execute("SELECT COUNT(*) FROM tasks WHERE chat_id = %s", (chat_id,))
        total = cur.fetchone()[0]
        if total == 0:
            return "You have no tasks to clear 😄"
        return f"⚠️ This will delete all {total} of your tasks.\nSend /doneall yes to confirm."

    elif text == "/doneall yes":
        cur.execute("DELETE FROM tasks WHERE chat_id = %s RETURNING gcal_event_id", (chat_id,))
        deleted = cur.fetchall()
        sync_deleted(cur, [row[0] for row in deleted])
        return f"Cleared 🧹 {len(deleted)} tasks deleted."

    elif text == "/done":
        return "What should I delete? Type it like this:\n/done 2"

    elif text.startswith("/done "):
        num_text = text[6:]
        cur.execute(f"SELECT id FROM tasks WHERE chat_id = %s {TASK_ORDER}", (chat_id,))
        rows = cur.fetchall()
        if num_text.isdigit() and 1 <= int(num_text) <= len(rows):
            real_id = rows[int(num_text) - 1][0]
            cur.execute("DELETE FROM tasks WHERE id = %s AND chat_id = %s RETURNING gcal_event_id", (real_id, chat_id))
            sync_deleted(cur, [row[0] for row in cur.fetchall()])
            return "Deleted 🗑️"
        return "No task with that number 🤔"

    elif text == "/edit":
        return "Which one? Type it like this:\n/edit 2\n\nOr tap ✏️ next to a task in 📋 My tasks."

    elif text.startswith("/edit "):
        num_text = text[6:]
        cur.execute(f"SELECT id FROM tasks WHERE chat_id = %s {TASK_ORDER}", (chat_id,))
        rows = cur.fetchall()
        if num_text.isdigit() and 1 <= int(num_text) <= len(rows):
            return start_edit(cur, chat_id, rows[int(num_text) - 1][0])
        return "No task with that number 🤔"

    elif text == "/cancel":
        return "Nothing to cancel 🙂"

    # Owner-only. For anyone else these fall through to "I don't know that command".
    elif text == "/connect" and is_owner(chat_id):
        return connect_message(cur, chat_id)

    elif text == "/sync" and is_owner(chat_id):
        if not g.get("gacct"):
            return "Google sync isn't on. Send /connect first."
        google_safely(cur, "full sync", gsync.push_unsynced, g.gacct, True)
        return "🔄 Synced with Google Calendar."

    elif text == "/disconnect" and is_owner(chat_id):
        return disconnect(cur, chat_id)

    elif text.startswith("/"):
        return "I don't know that command 🤔 Tap ❓ Help to see what I can do."

    else:
        # Anything else is a new task (or several, one per line).
        return add_tasks(cur, chat_id, text)


def handle_callback(cur, chat_id, data):
    """A tap on an inline button. Returns (popup_text, new_text, new_buttons, new_message).
    new_text None means "leave the tapped message as it is".
    new_message is an extra message to send (or None)."""
    # callback_data looks like "undo:57" -> action "undo", task id 57
    action, _, task_id = data.partition(":")

    if action == "undo" and "-" in task_id:
        # "undo:61-64" = the "Undo all" button after saving several lines at once
        first, _, last = task_id.partition("-")
        if first.isdigit() and last.isdigit() and 0 <= int(last) - int(first) <= 100:
            cur.execute("DELETE FROM tasks WHERE id BETWEEN %s AND %s AND chat_id = %s RETURNING gcal_event_id",
                        (int(first), int(last), chat_id))
            deleted = cur.fetchall()
            if not deleted:
                return "Already removed", None, None, None
            sync_deleted(cur, [row[0] for row in deleted])
            return "Undone ↩️", f"↩️ Removed {len(deleted)} tasks.", None, None

    if not task_id.isdigit():
        return "This button is old. Send /list again 🙂", None, None, None
    task_id = int(task_id)

    if action == "undo":
        # chat_id in the WHERE means you can only ever delete your own task.
        cur.execute("DELETE FROM tasks WHERE id = %s AND chat_id = %s RETURNING task, gcal_event_id", (task_id, chat_id))
        row = cur.fetchone()
        if row is None:
            return "Already removed", None, None, None
        sync_deleted(cur, [row[1]])
        return "Undone ↩️", f"↩️ Removed: {row[0]}", None, None

    if action == "edit":
        instructions = start_edit(cur, chat_id, task_id)
        if instructions is None:
            return "That task is already gone", None, None, None
        return "Editing ✏️", None, None, instructions

    if action in ("done", "rdone"):
        cur.execute("DELETE FROM tasks WHERE id = %s AND chat_id = %s RETURNING task, gcal_event_id", (task_id, chat_id))
        row = cur.fetchone()
        popup = f"Done ✅ {short(row[0])}" if row else "Already done"
        if row:
            sync_deleted(cur, [row[1]])

        if action == "done":
            # Tapped on the task list: show the list again, minus that task.
            text, buttons = list_view(cur, chat_id)
            return popup, text, buttons, None

        # Tapped on the morning reminder: show the reminder again, minus that task.
        today = india_today()
        cur.execute(f"SELECT id, task, due, due_time FROM tasks WHERE chat_id = %s AND due <= %s {TASK_ORDER}",
                    (chat_id, today + timedelta(days=1)))
        rows = cur.fetchall()
        if not rows:
            return popup, "All done for now 🎉 Nice work!", None, None
        text, buttons = reminder_view(rows, today)
        return popup, text, buttons, None

    return "This button is old. Send /list again 🙂", None, None, None


def warn_if_sync_broke(chat_id):
    """If Google refused our saved sign-in (access removed, or expired), tell the owner once."""
    acct = g.get("gacct")
    if acct and acct.get("broken"):
        tg("sendMessage", chat_id=chat_id,
           text="⚠️ Google Calendar sync stopped: Google no longer accepts the sign-in. Send /connect to turn it back on.")


def on_message(message):
    chat_id = message["chat"]["id"]
    text = message["text"].strip()
    first_name = message.get("from", {}).get("first_name")

    with connect() as conn:
        cur = conn.cursor()
        editing_id = save_user(cur, chat_id, first_name)
        start_sync(cur, chat_id)
        reply = handle_message(cur, chat_id, text, editing_id)
        conn.commit()
    warn_if_sync_broke(chat_id)

    markup = None
    if isinstance(reply, tuple):
        reply, markup = reply

    # A message can only carry one keyboard. If there are no inline buttons,
    # send the main keyboard again so it's always there.
    if markup:
        markup = {"inline_keyboard": markup}
    else:
        markup = MAIN_KEYBOARD
    tg("sendMessage", chat_id=chat_id, text=reply, reply_markup=markup,
       link_preview_options={"is_disabled": True})


def on_callback(cq):
    # The chat the button's message lives in. In a private chat this is the user.
    message = cq.get("message")
    chat_id = message["chat"]["id"] if message else cq["from"]["id"]
    data = cq.get("data", "")

    with connect() as conn:
        cur = conn.cursor()
        save_user(cur, chat_id, cq["from"].get("first_name"))
        start_sync(cur, chat_id)
        popup, new_text, buttons, new_message = handle_callback(cur, chat_id, data)
        conn.commit()
    warn_if_sync_broke(chat_id)

    # Always answer, or the button shows a loading spinner for a long time.
    tg("answerCallbackQuery", callback_query_id=cq["id"], text=popup)

    if new_text is not None and message:
        params = {"chat_id": chat_id, "message_id": message["message_id"], "text": new_text}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        tg("editMessageText", **params)

    if new_message:
        tg("sendMessage", chat_id=chat_id, text=new_message, reply_markup=MAIN_KEYBOARD)


@app.route("/")
def home():
    return "Deadline Buddy is alive! 🤖"


# Google needs a privacy policy link before an app that uses Google sign-in can be published.
PRIVACY_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deadline Buddy – Privacy</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;line-height:1.6}</style>
</head><body>
<h1>Deadline Buddy – Privacy policy</h1>
<p>Deadline Buddy is a small personal Telegram bot that remembers tasks and deadlines.</p>
<h2>What it stores</h2>
<ul>
<li>Your Telegram chat id and first name, so your tasks stay yours.</li>
<li>The tasks you send it, with their dates and times.</li>
<li>A random secret for your private calendar link, if you ask for one.</li>
<li>For the owner only: a Google sign-in token used to keep one calendar named
"Deadline Buddy" in sync. The bot cannot see or change any other calendar.</li>
</ul>
<h2>What it does with it</h2>
<p>Only shows your tasks back to you, sends your morning reminder, and builds your calendar.
Nothing is sold, shared, or used for ads.</p>
<h2>Deleting your data</h2>
<p>Send <code>/doneall yes</code> to delete all your tasks, or <code>/calendar reset</code> to turn off your
calendar link. Google access can be removed at any time from your Google Account's
"Third-party connections" page.</p>
</body></html>"""


@app.route("/privacy")
def privacy():
    return Response(PRIVACY_PAGE, mimetype="text/html")


@app.route("/webhook", methods=["POST"])
def webhook():
    # Only checked once WEBHOOK_SECRET is set, so the bot keeps working while it's being set up.
    # compare_digest compares safely (it takes the same time whether the guess is close or not).
    if WEBHOOK_SECRET:
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(sent, WEBHOOK_SECRET):
            return "Not allowed", 401

    update = request.get_json(silent=True) or {}

    # If anything below crashes we still return "ok". Otherwise Telegram keeps
    # re-sending the same update forever and the bot looks dead.
    try:
        if "callback_query" in update:
            on_callback(update["callback_query"])
        elif "message" in update and "text" in update["message"]:
            on_message(update["message"])
    except Exception:
        print("Error handling update:", safe(traceback.format_exc()))
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("id"):
            tg("sendMessage", chat_id=chat["id"], text="Oops, something went wrong 😵 Please try again.")
        elif "callback_query" in update:
            tg("answerCallbackQuery", callback_query_id=update["callback_query"]["id"], text="Oops, something went wrong 😵")

    if DRY_RUN:
        return jsonify(g.get("sent", []))
    return "ok"


def small_page(title, text, status=200):
    html = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, "
            f"initial-scale=1'><title>{title}</title><style>body{{font-family:system-ui,sans-serif;max-width:480px;"
            f"margin:3rem auto;padding:0 1rem;line-height:1.6}}</style></head><body><h1>{title}</h1><p>{text}</p></body></html>")
    return Response(html, status=status, mimetype="text/html")


@app.route("/oauth/callback")
def oauth_callback():
    """Google sends the owner's browser here after they press Allow (or Cancel)."""
    if request.args.get("error"):
        return small_page("Not connected", "Google sign-in was cancelled. Send /connect in Telegram to try again.", 400)
    state, code = request.args.get("state", ""), request.args.get("code", "")

    with connect() as conn:
        cur = conn.cursor()
        # The state must be one we made in the last 15 minutes, for the owner. Then it's used up.
        cur.execute("SELECT chat_id FROM google_accounts WHERE oauth_state = %s "
                    "AND oauth_state_at > now() - interval '15 minutes'", (state,))
        row = cur.fetchone()
        if not state or not code or row is None or not is_owner(row[0]):
            return small_page("Link expired", "This sign-in link is old or not valid. Send /connect in Telegram again.", 400)
        chat_id = row[0]
        cur.execute("UPDATE google_accounts SET oauth_state = NULL WHERE chat_id = %s", (chat_id,))
        conn.commit()

        tokens = gsync.exchange_code(code, OAUTH_REDIRECT)
        if not tokens or not tokens.get("refresh_token"):
            return small_page("Something went wrong", "Google didn't give the bot access. Send /connect and try again.", 500)
        if gsync.SCOPE not in tokens.get("scope", "").split():
            gsync.revoke(tokens["refresh_token"])
            return small_page("Calendar box not ticked",
                              "The bot needs the calendar permission. Send /connect again and tick the calendar box.", 400)
        try:
            with conn.transaction():
                count = gsync.finish_connect(cur, chat_id, tokens)
        except Exception:
            print("Finishing Google connect failed:", safe(traceback.format_exc()))
            count = None
        conn.commit()

    if count is None:
        tg("sendMessage", chat_id=chat_id, text="⚠️ Signed in, but I couldn't create the calendar. Try /connect again.")
        return small_page("Almost", "Signed in, but creating the calendar failed. Send /connect in Telegram to retry.", 500)
    tg("sendMessage", chat_id=chat_id, reply_markup=MAIN_KEYBOARD, text=(
        "✅ Google Calendar connected!\n\n"
        f"I made a calendar called \"Deadline Buddy\" and put {count} task(s) in it.\n"
        "• Add, edit or ✅ here → it changes in Google right away\n"
        "• Move, rename or delete an event there → I pick it up next time you message me\n"
        "• Timed tasks get a phone alert 30 minutes before\n\n"
        "Tip: remove the old 📅 link subscription from Google Calendar, or you'll see every task twice."))
    return small_page("Connected ✅", "Google Calendar is connected. You can close this tab and go back to Telegram.")


@app.route("/cal/<token>.ics")
def calendar_feed(token):
    # Google Calendar fetches this URL every few hours. The token is the only
    # "password", so an unknown token gets a plain 404.
    if len(token) > 64:
        return "Not found", 404
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM users WHERE cal_token = %s", (token,))
        user = cur.fetchone()
        if user is None:
            return "Not found", 404
        cur.execute(f"SELECT id, task, due, due_time FROM tasks WHERE chat_id = %s AND due IS NOT NULL {TASK_ORDER}", (user[0],))
        rows = cur.fetchall()

    return Response(
        make_ics(rows, datetime.now(timezone.utc)),
        mimetype="text/calendar",
        headers={"Cache-Control": "no-cache", "Content-Disposition": 'inline; filename="deadline-buddy.ics"'},
    )


@app.route("/cron/daily")
def daily_reminder():
    secret = os.getenv("CRON_SECRET")
    if not secret or request.headers.get("Authorization") != f"Bearer {secret}":
        return "Not allowed", 401

    today = india_today()

    with connect() as conn:
        cur = conn.cursor()
        if OWNER_CHAT_ID:
            # Catch up with Google first (so the reminder is right), and retry anything that failed to push.
            start_sync(cur, int(OWNER_CHAT_ID))
            if g.get("gacct"):
                google_safely(cur, "catch-up push", gsync.push_unsynced, g.gacct)
            conn.commit()
        cur.execute("SELECT chat_id, id, task, due, due_time FROM tasks WHERE due <= %s "
                    "ORDER BY chat_id, due, due_time NULLS FIRST, id", (today + timedelta(days=1),))
        rows = cur.fetchall()

    # Group the rows by user: {chat_id: [(id, task, due, due_time), ...]}
    per_user = {}
    for chat_id, task_id, task, due, due_time in rows:
        per_user.setdefault(chat_id, []).append((task_id, task, due, due_time))

    for chat_id, user_rows in per_user.items():
        text, buttons = reminder_view(user_rows, today)
        tg("sendMessage", chat_id=chat_id, text=text, reply_markup={"inline_keyboard": buttons})

    if DRY_RUN:
        return jsonify(g.get("sent", []))
    return f"Sent {len(per_user)} reminders"
