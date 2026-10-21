# Deadline Buddy ⏳

A Telegram bot that remembers your assignments, tests and deadlines.
Open it in Telegram, then just type what you need to do.

## What it does

- **Type a task in plain English.** `chem record friday`, `physics test 25 oct`,
  `submit lab tomorrow`, `pay fees next monday`. The bot finds the date, removes it
  from the task name, and saves it. Normal numbers like `chapter 5 notes` or `lab 3`
  are left alone.
- **Times too.** `call mom friday 5pm`, `viva 9:30 am on 3rd nov`, `lunch at noon`.
  A time with no day means today (or tomorrow if that time already passed).
- **Undo and Edit.** Every "Saved ✅" reply has ↩️ Undo and ✏️ Edit buttons. To edit,
  send a new day/time (`monday 5pm`), a new name, both, or `no date`.
- **Buttons, no commands needed.** A keyboard under the typing box:
  📋 My tasks · ➕ Add · 📅 Calendar · ❓ Help
- **Tap to finish.** The task list has a ✅ button per task (and ✏️ to change it).
  Tapping ✅ deletes the task and updates the list in place.
- **Morning reminder.** Every day at 5:30 AM IST the bot sends what's overdue, due
  today and due tomorrow, with ✅ buttons.
- **Google Calendar.** 📅 Calendar gives you a private link. Add it in Google
  Calendar (Other calendars → + → From URL) and tasks **with a date** show up (all-day,
  or at their time). Tasks without a date stay in the bot only. `/calendar reset`
  makes a new link if you leaked the old one.
- **Two-way Google sync (owner only).** The owner can `/connect` their Google account.
  The bot then keeps its own "Deadline Buddy" calendar in sync both ways: changes in the
  bot show up in Google right away, and events moved, renamed, deleted or added in that
  calendar come back into the bot. Timed tasks get a phone alert 30 minutes before. It
  uses the `calendar.app.created` scope, so the bot can't see or touch any other calendar.
  Owner commands: `/connect`, `/sync`, `/disconnect`.
- **Private.** Every query filters by your chat id, so you only ever see or change
  your own tasks.

Backup commands: `/add <task> @ <date>`, `/list`, `/edit <n>`, `/cancel`, `/done <n>`,
`/doneall`, `/calendar`, `/calendar reset`, `/help`.

## How it works

```
Telegram ──webhook──▶ Vercel (Flask, app.py) ──▶ Neon Postgres
Google Calendar ──GET /cal/<token>.ics──▶ Vercel
Vercel cron (00:00 UTC) ──GET /cron/daily──▶ Vercel ──▶ Telegram
```

| File | What's in it |
|---|---|
| `app.py` | The Flask app: webhook, buttons, commands, calendar feed, morning cron |
| `dates.py` | Finds a date and time inside a sentence (`extract_when`) |
| `icsfeed.py` | Builds the `.ics` calendar text (`make_ics`) |
| `gsync.py` | Owner's two-way Google Calendar sync (OAuth sign-in, push, pull) |
| `test_gsync.py` | Tests for the Google event conversion and sign-in link |
| `test_dates.py` | 47 example sentences for the date and time finder |
| `test_ics.py` | Checks the calendar feed is valid, using the `icalendar` package |
| `fake_telegram.py` | Pretends to be Telegram for local testing |
| `vercel.json` | The daily cron job |

Database tables:

- `tasks(id, chat_id, task, due, due_time, gcal_event_id)`
- `users(chat_id, first_name, created_at, cal_token, editing_task_id)`
- `google_accounts(chat_id, refresh_token, access_token, access_expires, calendar_id, sync_token, oauth_state, oauth_state_at)`

Tables are created automatically (`CREATE TABLE IF NOT EXISTS`) the first time the
app connects.

## Running it locally (Windows PowerShell)

`.env` needs `BOT_TOKEN`, `DATABASE_URL` and `CRON_SECRET` (never commit it).

```powershell
pip install -r requirements.txt -r requirements-dev.txt
python test_dates.py
python test_ics.py
python test_gsync.py

# Terminal 1: start the bot. DRY_RUN=1 means it won't really message Telegram.
$env:DRY_RUN="1"; flask --app app run

# Terminal 2: pretend to be Telegram
python fake_telegram.py "chem record friday"
python fake_telegram.py "📋 My tasks"
python fake_telegram.py --tap done:12
```

The local bot uses the real database, but `fake_telegram.py` uses a fake chat id
(999000001), so your tests never mix with real users' tasks.

## Deploying

`git push` to `main`. Vercel deploys automatically.
Environment variables in Vercel: `BOT_TOKEN`, `DATABASE_URL`, `CRON_SECRET`,
`WEBHOOK_SECRET` (optional: `BASE_URL`, defaults to `https://deadline-buddy-one.vercel.app`).

For the owner's Google sync: `OWNER_CHAT_ID` (the owner's Telegram chat id),
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (a Google Cloud OAuth "Web application"
client with redirect URI `https://deadline-buddy-one.vercel.app/oauth/callback`).

`WEBHOOK_SECRET` must be the same in `.env`, in Vercel, and in Telegram's webhook
settings (`setWebhook` with `secret_token`). If you ever change it, update Vercel and
redeploy first, then call `setWebhook` again.
