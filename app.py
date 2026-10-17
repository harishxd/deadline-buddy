import os
import requests
import psycopg
import dateparser
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
URL = f"https://api.telegram.org/bot{TOKEN}/"

app = Flask(__name__)


def handle_message(cur, chat_id, text):
    if text.lower() == "hi":
        return "Hello buddy 👋"

    elif text in ["/start", "/help"]:
        return "Hi! I'm Deadline Buddy 📅\n\nI remember your tasks so you don't have to.\n\n/add <task> – save a task\n/list – see your tasks\n/done <number> – remove a task"

    elif text == "/add":
        return "What should I save? Type it like this:\n/add Chemistry record"

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
            return f"Saved ✅ {task}\n📅 Due {due.strftime('%a, %d %b')}"
        return "Saved ✅ " + task

    elif text == "/list":
        cur.execute("SELECT task, due FROM tasks WHERE chat_id = %s ORDER BY due NULLS LAST, id", (chat_id,))
        rows = cur.fetchall()
        if row[1]:
            reply += f"{count}. {row[0]} — 📅 {row[1].strftime('%a, %d %b')}\n"
        else:
            reply += f"{count}. {row[0]}\n"
        reply = "Your tasks:\n"
        count = 0
        for row in rows:
            count += 1
            reply += f"{count}. {row[1]}\n"
        return reply

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