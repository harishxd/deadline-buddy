import os
import traceback
import requests
import psycopg
import dateparser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, g, jsonify
from dotenv import load_dotenv

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


def nice_date(d):
    if d.year == datetime.now(ZoneInfo("Asia/Kolkata")).year:
        return d.strftime('%a, %d %b')
    return d.strftime('%a, %d %b %Y')


def handle_message(cur, chat_id, text):
    if text.lower() == "hi":
        return "Hello buddy 👋"

    elif text in ["/start", "/help", BTN_HELP]:
        return HELP_TEXT

    elif text == "/add":
        return "What should I save? Type it like this:\n/add Physics test @ 25 Oct"

    elif text.startswith("/add "):
        task, _, date_text = text[5:].partition("@")
        task = task.strip()
        date_text = date_text.strip()
        if not task:
            return "What's the task? Type it like this:\n/add Physics test @ 25 Oct"
        due = None
        if date_text:
            now_india = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
            parsed = dateparser.parse(date_text, settings={"PREFER_DATES_FROM": "future", "DATE_ORDER": "DMY", "RELATIVE_BASE": now_india})
            if parsed is None:
                return "I couldn't understand that date 🤔 Try: /add Physics test @ 25 Oct"
            due = parsed.date()
        cur.execute("INSERT INTO tasks (chat_id, task, due) VALUES (%s, %s, %s)", (chat_id, task, due))
        if due:
            return f"Saved ✅ {task}\n📅 Due {nice_date(due)}"
        return "Saved ✅ " + task

    elif text in ["/list", BTN_LIST]:
        cur.execute("SELECT task, due FROM tasks WHERE chat_id = %s ORDER BY due NULLS LAST, id", (chat_id,))
        rows = cur.fetchall()
        if not rows:
            return "No tasks yet. Add one with /add"
        reply = "Your tasks:\n"
        count = 0
        for row in rows:
            count += 1
            if row[1]:
                reply += f"{count}. {row[0]} — 📅 {nice_date(row[1])}\n"
            else:
                reply += f"{count}. {row[0]}\n"
        return reply

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

    else:
        return "You said: " + text


def handle_callback(cur, chat_id, data):
    """A tap on an inline button. Returns (popup_text, new_text, new_buttons).
    new_text None means "leave the message as it is"."""
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

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, task, due FROM tasks WHERE due <= %s ORDER BY chat_id, due, id", (today + timedelta(days=1),))
        rows = cur.fetchall()

    messages = {}
    for chat_id, task, due in rows:
        if due < today:
            label = "⚠️ Overdue"
        elif due == today:
            label = "🔥 Today"
        else:
            label = "⏰ Tomorrow"
        messages.setdefault(chat_id, []).append(f"{label}: {task}")

    for chat_id, lines in messages.items():
        text = "Good morning! ☀️ Here's what's due:\n\n" + "\n".join(lines)
        tg("sendMessage", chat_id=chat_id, text=text)

    return f"Sent {len(messages)} reminders"
