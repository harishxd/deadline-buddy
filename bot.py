import requests
import time
import sqlite3
import os


from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
URL = f"https://api.telegram.org/bot{TOKEN}/"

offset = 0   # remembers which messages we've already handled
conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.db"))
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, task TEXT)")

print("Bot is running...")

# Skip old messages that piled up while the bot was off
try:
    old = requests.get(URL + "getUpdates", timeout=10).json()["result"]
    if old:
        offset = old[-1]["update_id"] + 1
except Exception as e:
    print("Couldn't skip old messages:", e)

while True:
    # 1. Ask Telegram for new messages (if the internet fails, wait and retry)
    try:
        response = requests.get(URL + "getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
        updates = response.json()["result"]
    except Exception as e:
        print("Network problem, retrying in 5 sec...")
        time.sleep(5)
        continue

    for update in updates:
        offset = update["update_id"] + 1  # mark this message as handled

        # skip things that aren't text messages (stickers, photos...)
        if "message" not in update or "text" not in update["message"]:
            continue

        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"]
        print("Got:", text)

        if text.lower() == "hi":
            reply = "Hello buddy 👋"

        elif text in ["/start", "/help"]:
            reply = "Hi! I'm Deadline Buddy 📅\n\nI remember your tasks so you don't have to.\n\n/add <task> – save a task\n/list – see your tasks\n/done <number> – remove a task"


        elif text == "/add":
            reply = "What should I save? Type it like this:\n/add Chemistry record"

        elif text.startswith("/add "):
            task = text[5:]   # everything after "/add "
            cur.execute("INSERT INTO tasks (chat_id, task) VALUES (?, ?)", (chat_id, task))
            conn.commit()
            reply = "Saved ✅ " + task

        elif text == "/list":
            cur.execute("SELECT id, task FROM tasks WHERE chat_id = ? ORDER BY id", (chat_id,))
            rows = cur.fetchall()
            if rows:
                reply = "Your tasks:\n"
                count = 0
                for row in rows:
                    count += 1
                    reply += f"{count}. {row[1]}\n"
            else:
                reply = "No tasks yet. Add one with /add"
        elif text == "/done":
            reply = "What should I delete? Type it like this:\n/done 2"

        elif text.startswith("/done "):
            num_text = text[6:]
            cur.execute("SELECT id FROM tasks WHERE chat_id = ? ORDER BY id", (chat_id,))
            rows = cur.fetchall()
            if num_text.isdigit() and 1 <= int(num_text) <= len(rows):
                real_id = rows[int(num_text) - 1][0]
                cur.execute("DELETE FROM tasks WHERE id = ?", (real_id,))
                conn.commit()
                reply = "Deleted 🗑️"
            else:
                reply = "No task with that number 🤔"
        else:
            reply = "You said: " + text

        # 2. Reply (if it fails, don't crash)
        try:
            requests.get(URL + "sendMessage", params={"chat_id": chat_id, "text": reply}, timeout=10)
        except Exception as e:
            print("Couldn't send reply")