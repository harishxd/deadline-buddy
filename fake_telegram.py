"""Pretend to be Telegram and send updates to the local bot.

Start the bot first (in another terminal, PowerShell):
    $env:DRY_RUN="1"; flask --app app run

Then:
    python fake_telegram.py "chem record friday"     # send a text message
    python fake_telegram.py "📋 My tasks"            # press a keyboard button
    python fake_telegram.py --tap done:12            # tap an inline button (callback_data)

With DRY_RUN=1 the bot doesn't really message Telegram. It sends back the
calls it *would* have made, and this script prints them.
"""
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv(".env")

CHAT_ID = 999000001  # a fake user, so tests never mix with real friends' tasks
BOT = "http://127.0.0.1:5000/webhook"
# Send the webhook secret like real Telegram does, or the bot rejects us.
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": os.getenv("WEBHOOK_SECRET", "")}


def send(update):
    r = requests.post(BOT, json=update, headers=HEADERS, timeout=30)
    try:
        calls = r.json()
    except ValueError:
        print(r.status_code, r.text)
        return
    for call in calls:
        print(f"--- {call['method']} ---")
        if "text" in call:
            print(call["text"])
        markup = call.get("reply_markup") or {}
        for row in markup.get("inline_keyboard", []):
            print("   ".join(f"[{b['text']}] -> {b['callback_data']}" for b in row))
        if "keyboard" in markup:
            print("(main keyboard shown)")


def message(text):
    send({"update_id": 1, "message": {
        "message_id": 1, "chat": {"id": CHAT_ID, "type": "private"},
        "from": {"id": CHAT_ID, "first_name": "Tester"}, "text": text}})


def tap(data):
    send({"update_id": 2, "callback_query": {
        "id": "fake-callback", "data": data,
        "from": {"id": CHAT_ID, "first_name": "Tester"},
        "message": {"message_id": 1, "chat": {"id": CHAT_ID, "type": "private"}}}})


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--tap":
        tap(sys.argv[2])
    else:
        message(" ".join(sys.argv[1:]) or "/list")
