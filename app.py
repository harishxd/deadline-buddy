import os
import requests
import psycopg
import dateparser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
URL = f"https://api.telegram.org/bot{TOKEN}/"

app = Flask(__name__)


def nice_date(d):
    if d.year == datetime.now(ZoneInfo("Asia/Kolkata")).year:
        return d.strftime('%a, %d %b')
    return d.strftime('%a, %d %b %Y')


def handle_message(cur, chat_id, text):
    if text.lower() == "hi":
        return "Hello buddy 👋"

    elif text in ["/start", "/help"]:
        return "Hi! I'm Deadline Buddy ⏳\n\nI remember your tasks so you don't have to.\n\n/add <task> – save a task\n/add <task> @ <date> – save with a deadline\n/list – see your tasks\n/done <number> – remove a task\n/doneall – clear all tasks"

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

    elif text == "/list":
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
            cur.execute("DELETE FROM tasks WHERE id = %s", (real_id,))
            return "Deleted 🗑️"
        return "No task with that number 🤔"

    else:
        return "You said: " + text


@app.route("/")
def home():
    return "Deadline Buddy is alive! 🤖"


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()

    if "message" not in update or "text" not in update["message"]:
        return "ok"

    chat_id = update["message"]["chat"]["id"]
    text = update["message"]["text"]

    with psycopg.connect(os.getenv("DATABASE_URL"), connect_timeout=10) as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS tasks (id SERIAL PRIMARY KEY, chat_id BIGINT, task TEXT)")
        cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due DATE")
        reply = handle_message(cur, chat_id, text)
        conn.commit()

    try:
        requests.get(URL + "sendMessage", params={"chat_id": chat_id, "text": reply}, timeout=10)
    except Exception:
        print("Couldn't send reply")

    return "ok"

@app.route("/cron/daily")
def daily_reminder():
    secret = os.getenv("CRON_SECRET")
    if not secret or request.headers.get("Authorization") != f"Bearer {secret}":
        return "Not allowed", 401

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    with psycopg.connect(os.getenv("DATABASE_URL"), connect_timeout=10) as conn:
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
        try:
            requests.get(URL + "sendMessage", params={"chat_id": chat_id, "text": text}, timeout=10)
        except Exception:
            print("Couldn't send to", chat_id)

    return f"Sent {len(messages)} reminders"