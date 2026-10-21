import os
import traceback
import requests
import psycopg
import dateparser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, g, jsonify
from dotenv import load_dotenv
from dates import extract_date, clean

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
URL = f"https://api.telegram.org/bot{TOKEN}/"

# Local testing: with DRY_RUN=1 we don't call Telegram at all. The calls we
# *would* make are returned from /webhook so fake_telegram.py can print them.
DRY_RUN = os.getenv("DRY_RUN") == "1"

app = Flask(__name__)

# The big grey buttons under the typing box. They just send their label as a
# normal text message, so handle_message() checks for these exact strings.
BTN_LIST = "📋 My tasks"
BTN_ADD = "➕ Add"
BTN_CAL = "📅 Calendar"
BTN_HELP = "❓ Help"

MAIN_KEYBOARD = {
    "keyboard": [[BTN_LIST, BTN_ADD], [BTN_CAL, BTN_HELP]],
    "resize_keyboard": True,
    "is_persistent": True,
}

HELP_TEXT = (
    "Hi! I'm Deadline Buddy ⏳\n\n"
    "I remember your tasks so you don't have to.\n\n"
    "Just type a task, like:\n"
    "• chem record friday\n"
    "• physics test 25 oct\n"
    "• submit lab tomorrow\n\n"
    "Buttons below:\n"
    f"{BTN_LIST} – see your tasks, tap ✅ to finish one\n"
    f"{BTN_CAL} – see your deadlines in Google Calendar\n\n"
    "Commands also work:\n"
    "/add <task> @ <date> – save a task\n"
    "/list – see your tasks\n"
    "/done <number> – remove a task\n"
    "/doneall – clear all tasks\n"
    "/calendar – get your calendar link\n\n"
    "☀️ Every morning I'll remind you what's overdue, due today and due tomorrow."
)


def safe(text):
    """Hide secrets before anything gets printed to the logs."""
    text = str(text)
    for secret in (TOKEN, os.getenv("DATABASE_URL"), os.getenv("CRON_SECRET")):
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
            cur.execute("CREATE INDEX IF NOT EXISTS tasks_chat_id_idx ON tasks (chat_id)")
            cur.execute("CREATE TABLE IF NOT EXISTS users (chat_id BIGINT PRIMARY KEY, first_name TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cal_token TEXT")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_cal_token_idx ON users (cal_token)")
            conn.commit()
        except Exception:
            conn.close()
            raise
        _db_ready = True
    return conn


def save_user(cur, chat_id, first_name):
    # "Upsert": add the user the first time, update their name after that.
    cur.execute(
        "INSERT INTO users (chat_id, first_name) VALUES (%s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET first_name = EXCLUDED.first_name",
        (chat_id, first_name),
    )


def india_today():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def nice_date(d):
    if d.year == india_today().year:
        return d.strftime('%a, %d %b')
    return d.strftime('%a, %d %b %Y')


GREETINGS = {"hi", "hii", "hello", "hey", "heyy", "yo", "hola", "namaste", "thanks", "thank you",
             "thx", "ty", "ok", "okay", "k", "cool", "nice", "good morning", "gm", "good night", "gn", "bye"}


def add_task(cur, chat_id, text):
    """Save a task typed as normal text, e.g. "chem record friday"."""
    today = india_today()
    if "@" in text:
        # Old style: "task @ date". Everything after @ is the date.
        task, _, date_text = text.partition("@")
        task = clean(task)
        leftover, due = extract_date(date_text, today)
        if due is None and date_text.strip():
            # Our patterns didn't know it. Let dateparser have a go ("in 2 months" etc).
            now_india = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
            parsed = dateparser.parse(date_text, settings={"PREFER_DATES_FROM": "future", "DATE_ORDER": "DMY", "RELATIVE_BASE": now_india})
            if parsed is None:
                return "I couldn't understand that date 🤔 Try: physics test 25 oct"
            due = parsed.date()
    else:
        task, due = extract_date(text, today)

    if not task:
        return "What's the task? Type it like this:\nchem record friday"

    # RETURNING id gives us the new row's id, so the Undo button knows what to delete.
    cur.execute("INSERT INTO tasks (chat_id, task, due) VALUES (%s, %s, %s) RETURNING id", (chat_id, task, due))
    task_id = cur.fetchone()[0]

    reply = f"Saved ✅ {task}"
    if due:
        reply += f"\n📅 {nice_date(due)}"
    return reply, [[{"text": "↩️ Undo", "callback_data": f"undo:{task_id}"}]]


def short(task, size=28):
    """Cut long task names so they fit on a button."""
    return task if len(task) <= size else task[:size - 1] + "…"


def list_view(cur, chat_id):
    """The task list text plus one ✅ button per task. Used by /list and after a ✅ tap."""
    cur.execute("SELECT id, task, due FROM tasks WHERE chat_id = %s ORDER BY due NULLS LAST, id", (chat_id,))
    rows = cur.fetchall()
    if not rows:
        return "No tasks left 🎉\nJust type one, like: chem record friday", None

    today = india_today()
    lines = ["📋 Your tasks:", ""]
    buttons = []
    for number, (task_id, task, due) in enumerate(rows, start=1):
        line = f"{number}. {task}"
        if due:
            line += f" — 📅 {nice_date(due)}"
            if due < today:
                line += " ⚠️"
        lines.append(line)
        if len(buttons) < 50:  # Telegram allows 100 buttons per message
            # The button carries the real database id, not the list number.
            buttons.append([{"text": f"✅ {number}. {short(task)}", "callback_data": f"done:{task_id}"}])
    lines += ["", "Tap ✅ when you finish one."]
    return "\n".join(lines)[:4000], buttons  # Telegram's limit is 4096 characters


def reminder_view(rows, today):
    """Morning reminder text and buttons for one user's (id, task, due) rows."""
    lines = ["Good morning! ☀️ Here's what's due:", ""]
    buttons = []
    for task_id, task, due in rows:
        if due < today:
            label = "⚠️ Overdue"
        elif due == today:
            label = "🔥 Today"
        else:
            label = "⏰ Tomorrow"
        lines.append(f"{label}: {task}")
        buttons.append([{"text": f"✅ {short(task)}", "callback_data": f"rdone:{task_id}"}])
    return "\n".join(lines)[:4000], buttons[:50]


def handle_message(cur, chat_id, text):
    if text.lower().strip("!.? ") in GREETINGS:
        return "Hello buddy 👋 Just type a task, like: chem record friday"

    elif text in ["/start", "/help", BTN_HELP]:
        return HELP_TEXT

    elif text in ["/add", BTN_ADD]:
        return "Just type your task and when it's due 👇\n\nchem record friday\nphysics test 25 oct\nsubmit lab tomorrow"

    elif text.startswith("/add "):
        return add_task(cur, chat_id, text[5:])

    elif text in ["/calendar", BTN_CAL]:
        return "Calendar is coming soon 📅"

    elif text in ["/list", BTN_LIST]:
        return list_view(cur, chat_id)

    elif text == "/doneall":
        cur.execute("SELECT COUNT(*) FROM tasks WHERE chat_id = %s", (chat_id,))
        total = cur.fetchone()[0]
        if total == 0:
            return "You have no tasks to clear 😄"
        return f"⚠️ This will delete all {total} of your tasks.\nSend /doneall yes to confirm."

    elif text == "/doneall yes":
        cur.execute("DELETE FROM tasks WHERE chat_id = %s", (chat_id,))
        return f"Cleared 🧹 {cur.rowcount} tasks deleted."

    elif text == "/done":
        return "What should I delete? Type it like this:\n/done 2"

    elif text.startswith("/done "):
        num_text = text[6:]
        cur.execute("SELECT id FROM tasks WHERE chat_id = %s ORDER BY due NULLS LAST, id", (chat_id,))
        rows = cur.fetchall()
        if num_text.isdigit() and 1 <= int(num_text) <= len(rows):
            real_id = rows[int(num_text) - 1][0]
            cur.execute("DELETE FROM tasks WHERE id = %s AND chat_id = %s", (real_id, chat_id))
            return "Deleted 🗑️"
        return "No task with that number 🤔"

    elif text.startswith("/"):
        return "I don't know that command 🤔 Tap ❓ Help to see what I can do."

    else:
        # Anything else is a new task.
        return add_task(cur, chat_id, text)


def handle_callback(cur, chat_id, data):
    """A tap on an inline button. Returns (popup_text, new_text, new_buttons).
    new_text None means "leave the message as it is"."""
    # callback_data looks like "undo:57" -> action "undo", task id 57
    action, _, task_id = data.partition(":")
    if not task_id.isdigit():
        return "This button is old. Send /list again 🙂", None, None
    task_id = int(task_id)

    if action == "undo":
        # chat_id in the WHERE means you can only ever delete your own task.
        cur.execute("DELETE FROM tasks WHERE id = %s AND chat_id = %s RETURNING task", (task_id, chat_id))
        row = cur.fetchone()
        if row is None:
            return "Already removed", None, None
        return "Undone ↩️", f"↩️ Removed: {row[0]}", None

    if action in ("done", "rdone"):
        cur.execute("DELETE FROM tasks WHERE id = %s AND chat_id = %s RETURNING task", (task_id, chat_id))
        row = cur.fetchone()
        popup = f"Done ✅ {short(row[0])}" if row else "Already done"

        if action == "done":
            # Tapped on the task list: show the list again, minus that task.
            text, buttons = list_view(cur, chat_id)
            return popup, text, buttons

        # Tapped on the morning reminder: show the reminder again, minus that task.
        today = india_today()
        cur.execute("SELECT id, task, due FROM tasks WHERE chat_id = %s AND due <= %s ORDER BY due, id",
                    (chat_id, today + timedelta(days=1)))
        rows = cur.fetchall()
        if not rows:
            return popup, "All done for now 🎉 Nice work!", None
        text, buttons = reminder_view(rows, today)
        return popup, text, buttons

    return "This button is old. Send /list again 🙂", None, None


def on_message(message):
    chat_id = message["chat"]["id"]
    text = message["text"].strip()
    first_name = message.get("from", {}).get("first_name")

    with connect() as conn:
        cur = conn.cursor()
        save_user(cur, chat_id, first_name)
        reply = handle_message(cur, chat_id, text)
        conn.commit()

    markup = None
    if isinstance(reply, tuple):
        reply, markup = reply

    # A message can only carry one keyboard. If there are no inline buttons,
    # send the main keyboard again so it's always there.
    if markup:
        markup = {"inline_keyboard": markup}
    else:
        markup = MAIN_KEYBOARD
    tg("sendMessage", chat_id=chat_id, text=reply, reply_markup=markup)


def on_callback(cq):
    # The chat the button's message lives in. In a private chat this is the user.
    message = cq.get("message")
    chat_id = message["chat"]["id"] if message else cq["from"]["id"]
    data = cq.get("data", "")

    with connect() as conn:
        cur = conn.cursor()
        save_user(cur, chat_id, cq["from"].get("first_name"))
        popup, new_text, buttons = handle_callback(cur, chat_id, data)
        conn.commit()

    # Always answer, or the button shows a loading spinner for a long time.
    tg("answerCallbackQuery", callback_query_id=cq["id"], text=popup)

    if new_text is not None and message:
        params = {"chat_id": chat_id, "message_id": message["message_id"], "text": new_text}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        tg("editMessageText", **params)


@app.route("/")
def home():
    return "Deadline Buddy is alive! 🤖"


@app.route("/webhook", methods=["POST"])
def webhook():
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


@app.route("/cron/daily")
def daily_reminder():
    secret = os.getenv("CRON_SECRET")
    if not secret or request.headers.get("Authorization") != f"Bearer {secret}":
        return "Not allowed", 401

    today = india_today()

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, id, task, due FROM tasks WHERE due <= %s ORDER BY chat_id, due, id", (today + timedelta(days=1),))
        rows = cur.fetchall()

    # Group the rows by user: {chat_id: [(id, task, due), ...]}
    per_user = {}
    for chat_id, task_id, task, due in rows:
        per_user.setdefault(chat_id, []).append((task_id, task, due))

    for chat_id, user_rows in per_user.items():
        text, buttons = reminder_view(user_rows, today)
        tg("sendMessage", chat_id=chat_id, text=text, reply_markup={"inline_keyboard": buttons})

    if DRY_RUN:
        return jsonify(g.get("sent", []))
    return f"Sent {len(per_user)} reminders"
