# Learning notes: how Deadline Buddy got buttons, smart dates and a calendar

These notes go through every new idea, **in the order it was built**. Each one has the
real code from this project. Open the file next to these notes and find the lines.

Contents

1. [A safe helper for calling Telegram](#1-a-safe-helper-for-calling-telegram)
2. [Never let one bad update crash the webhook](#2-never-let-one-bad-update-crash-the-webhook)
3. [Testing without Telegram: DRY_RUN and fake_telegram.py](#3-testing-without-telegram-dry_run-and-fake_telegrampy)
4. [Safe schema changes, run once per server](#4-safe-schema-changes-run-once-per-server)
5. [The users table and "upsert"](#5-the-users-table-and-upsert)
6. [Reply keyboards (the big grey buttons)](#6-reply-keyboards-the-big-grey-buttons)
7. [Inline keyboards and callback_data](#7-inline-keyboards-and-callback_data)
8. [callback_query updates](#8-callback_query-updates)
9. [answerCallbackQuery](#9-answercallbackquery)
10. [editMessageText](#10-editmessagetext)
11. [Finding a date inside a sentence](#11-finding-a-date-inside-a-sentence)
12. [Testing the date finder](#12-testing-the-date-finder)
13. [RETURNING id and the Undo button](#13-returning-id-and-the-undo-button)
14. [Ownership: chat_id in every WHERE](#14-ownership-chat_id-in-every-where)
15. [Tap-to-finish lists and reminders](#15-tap-to-finish-lists-and-reminders)
16. [ICS calendar feeds](#16-ics-calendar-feeds)
17. [Secret links with random tokens](#17-secret-links-with-random-tokens)
18. [Serving the feed from Flask](#18-serving-the-feed-from-flask)
19. [Validating the feed](#19-validating-the-feed)
20. [Times: "call mom friday 5pm"](#20-times-call-mom-friday-5pm)
21. [Editing a task: remembering a conversation](#21-editing-a-task-remembering-a-conversation)
22. [Phase 3: real Google Calendar sync, just for you (not built)](#22-phase-3-real-google-calendar-sync-just-for-you-not-built)
23. [The webhook secret, explained](#23-the-webhook-secret-explained)
24. [Ideas for later](#24-ideas-for-later)

---

## 1. A safe helper for calling Telegram

Before, every Telegram call was written out by hand. Now there's one function,
`tg()`, and everything goes through it (`app.py`):

```python
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
```

- `**params` collects any keyword arguments into a dict. So
  `tg("sendMessage", chat_id=5, text="hi")` posts `{"chat_id": 5, "text": "hi"}`.
- **The trap:** our URL is `https://api.telegram.org/bot<TOKEN>/sendMessage`. If the
  internet is down, `requests` raises an error whose message *contains that URL*.
  `print(e)` would put your bot token in the Vercel logs. So we print only the
  error's type name, like `ConnectionError`.
- There's also `safe()`, which replaces any secret with `***` before printing a
  crash report:

```python
def safe(text):
    """Hide secrets before anything gets printed to the logs."""
    text = str(text)
    for secret in (TOKEN, os.getenv("DATABASE_URL"), os.getenv("CRON_SECRET")):
        if secret:
            text = text.replace(secret, "***")
    return text
```

## 2. Never let one bad update crash the webhook

When Telegram sends you an update and gets an error back (like a 500 because your
code crashed), it **sends the same update again, and again**. New messages wait
behind it, and the bot looks dead.

So the webhook catches everything and always answers `"ok"`:

```python
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
        ...
    return "ok"
```

- `get_json(silent=True)` returns `None` instead of crashing if the body isn't JSON.
  `or {}` turns `None` into an empty dict.
- `traceback.format_exc()` is the full error report as text, so you can still read
  what went wrong in the Vercel logs (with secrets hidden by `safe()`).
- If it was a message, the user gets "Oops, something went wrong 😵" instead of silence.

## 3. Testing without Telegram: DRY_RUN and fake_telegram.py

Telegram can't reach `127.0.0.1` on your laptop, so locally we pretend to be Telegram.

```python
DRY_RUN = os.getenv("DRY_RUN") == "1"
```

When `DRY_RUN` is on, `tg()` doesn't call Telegram. It saves the call in `g.sent`
(`g` is Flask's "scratch space for this one request"), and the webhook sends the list
back:

```python
    if DRY_RUN:
        return jsonify(g.get("sent", []))
    return "ok"
```

`fake_telegram.py` builds an update that looks exactly like what Telegram sends, posts
it, and prints what the bot *would* have sent:

```python
def message(text):
    send({"update_id": 1, "message": {
        "message_id": 1, "chat": {"id": CHAT_ID, "type": "private"},
        "from": {"id": CHAT_ID, "first_name": "Tester"}, "text": text}})


def tap(data):
    send({"update_id": 2, "callback_query": {
        "id": "fake-callback", "data": data,
        "from": {"id": CHAT_ID, "first_name": "Tester"},
        "message": {"message_id": 1, "chat": {"id": CHAT_ID, "type": "private"}}}})
```

Try it (PowerShell, two terminals):

```powershell
$env:DRY_RUN="1"; flask --app app run
python fake_telegram.py "chem record friday"
python fake_telegram.py --tap undo:57
```

`CHAT_ID = 999000001` is a made-up user, so test tasks never mix with your friends'.
DRY_RUN also stops `/cron/daily` from messaging real friends while you test.

## 4. Safe schema changes, run once per server

A schema change must never lose data, and must be fine to run twice. Every statement
uses `IF NOT EXISTS`:

```python
        cur.execute("CREATE TABLE IF NOT EXISTS tasks (id SERIAL PRIMARY KEY, chat_id BIGINT, task TEXT)")
        cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due DATE")
        cur.execute("CREATE INDEX IF NOT EXISTS tasks_chat_id_idx ON tasks (chat_id)")
        cur.execute("CREATE TABLE IF NOT EXISTS users (chat_id BIGINT PRIMARY KEY, first_name TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cal_token TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_cal_token_idx ON users (cal_token)")
```

The old code ran these on **every** message, which is an extra trip to the database
each time. Now they run once per server process:

```python
_db_ready = False

def connect():
    global _db_ready
    conn = psycopg.connect(os.getenv("DATABASE_URL"), connect_timeout=10)
    if not _db_ready:
        ...  # the statements above
        _db_ready = True
    return conn
```

Vercel keeps a "warm" copy of your function running for a while, and module-level
variables like `_db_ready` survive between requests in that copy. A new ("cold") copy
starts with `False` and checks once.

- An **index** (`tasks_chat_id_idx`) is like the index at the back of a book. "Find
  all tasks for chat 123" no longer has to read every row.
- A **unique index** on `cal_token` means two users can never have the same token.

## 5. The users table and "upsert"

```sql
users(chat_id BIGINT PRIMARY KEY, first_name TEXT, created_at TIMESTAMPTZ DEFAULT now(), cal_token TEXT)
```

Every message saves the sender's first name:

```python
def save_user(cur, chat_id, first_name):
    # "Upsert": add the user the first time, update their name after that.
    cur.execute(
        "INSERT INTO users (chat_id, first_name) VALUES (%s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET first_name = EXCLUDED.first_name",
        (chat_id, first_name),
    )
```

- **Upsert** = "update or insert". `ON CONFLICT (chat_id)` says what to do if that
  chat_id is already there (it's the primary key, so there can't be two).
- `EXCLUDED` means "the row I tried to insert". So if someone changes their Telegram
  name, the new name is saved.
- `created_at` fills itself in (`DEFAULT now()`), and the upsert never changes it, so it
  stays the date they first used the bot.

Now you can see who's who in Neon:

```sql
SELECT u.first_name, COUNT(t.id) FROM users u LEFT JOIN tasks t USING (chat_id) GROUP BY u.first_name;
```

## 6. Reply keyboards (the big grey buttons)

There are **two kinds** of Telegram keyboard. This is the first kind:

```python
BTN_LIST = "📋 My tasks"
BTN_ADD = "➕ Add"
BTN_CAL = "📅 Calendar"
BTN_HELP = "❓ Help"

MAIN_KEYBOARD = {
    "keyboard": [[BTN_LIST, BTN_ADD], [BTN_CAL, BTN_HELP]],
    "resize_keyboard": True,
    "is_persistent": True,
}
```

- It replaces the phone's normal keyboard area.
- `keyboard` is a list of rows; each row is a list of buttons. So this is a 2×2 grid.
- `resize_keyboard` makes the buttons small instead of filling half the screen.
- `is_persistent` keeps it visible instead of hiding it after one tap.
- **Pressing a button just sends its label as a normal message.** There's nothing
  special about it. That's why `handle_message` checks the label text:

```python
    elif text in ["/list", BTN_LIST]:
        return list_view(cur, chat_id)
```

It's attached to replies through `reply_markup`. A message can only carry **one**
keyboard, so replies without inline buttons carry the main keyboard again. That's how
your existing friends get the keyboard on their next message without typing /start:

```python
    if markup:
        markup = {"inline_keyboard": markup}
    else:
        markup = MAIN_KEYBOARD
    tg("sendMessage", chat_id=chat_id, text=reply, reply_markup=markup,
       link_preview_options={"is_disabled": True})
```

(`link_preview_options` stops Telegram from showing a big preview box under links.)

## 7. Inline keyboards and callback_data

The second kind of keyboard is attached **to one message**, under its text:

```python
    return reply, [[{"text": "↩️ Undo", "callback_data": f"undo:{task_id}"},
                    {"text": "✏️ Edit", "callback_data": f"edit:{task_id}"}]]
```

| | Reply keyboard | Inline keyboard |
|---|---|---|
| Where | Instead of the phone keyboard | Under one message |
| Pressing it | Sends the label as a normal message | Sends a hidden `callback_query` |
| Shows in chat? | Yes, as if you typed it | No |
| Good for | Main menu | Actions on *this* message (Undo, ✅) |

- `text` is what the user sees.
- `callback_data` is a hidden string (up to 64 bytes) that comes back to you when
  they tap. We pack an action and a database id into it: `"undo:57"`, `"done:57"`,
  `"rdone:57"`.

## 8. callback_query updates

Tapping an inline button doesn't send a message. Telegram sends your webhook a
different kind of update:

```json
{"update_id": 2, "callback_query": {
    "id": "4382bfdwdsb323b2d9",
    "from": {"id": 123, "first_name": "Harish"},
    "message": {"message_id": 88, "chat": {"id": 123}, "text": "Saved ✅ ..."},
    "data": "undo:57"}}
```

So the webhook checks for both:

```python
        if "callback_query" in update:
            on_callback(update["callback_query"])
        elif "message" in update and "text" in update["message"]:
            on_message(update["message"])
```

And `handle_callback` splits the data back apart:

```python
    # callback_data looks like "undo:57" -> action "undo", task id 57
    action, _, task_id = data.partition(":")
    if not task_id.isdigit():
        return "This button is old. Send /list again 🙂", None, None
    task_id = int(task_id)
```

`"undo:57".partition(":")` gives `("undo", ":", "57")`. The `isdigit()` check matters:
old messages still have the earlier test button with `callback_data: "test"`, and
anyone can send any text, so never trust it blindly.

## 9. answerCallbackQuery

After a tap, the button shows a little loading clock until you answer. You
**must always** answer, even if there's nothing to say:

```python
    # Always answer, or the button shows a loading spinner for a long time.
    tg("answerCallbackQuery", callback_query_id=cq["id"], text=popup)
```

The `text` appears as a small popup at the top of the chat ("Done ✅ physics test").
`callback_query_id` is the `id` from the update, so Telegram knows which tap you mean.

## 10. editMessageText

Instead of sending a new list after every ✅ tap (and filling the chat with old lists),
we **change the message that was tapped**:

```python
    if new_text is not None and message:
        params = {"chat_id": chat_id, "message_id": message["message_id"], "text": new_text}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        tg("editMessageText", **params)
```

- `chat_id` + `message_id` say exactly which message to change. Both come from the
  callback_query.
- If you leave out `reply_markup`, the inline buttons disappear. That's how Undo
  removes its own button: the message becomes "↩️ Removed: physics test" with no buttons.
- If the new text is exactly the same as the old, Telegram replies "message is not
  modified". That's harmless; `tg()` just logs it.

## 11. Finding a date inside a sentence

The goal: `"pay fees next monday"` → task `"pay fees"`, due Monday.
The hard part is **not** finding dates that aren't there: `"chapter 5 notes"`,
`"lab 3"`, `"may i borrow notes"`.

### Why not just ask dateparser?

`dateparser.search` tries to find dates in *anything*, so "may" becomes May and "5"
becomes the 5th. Instead, `dates.py` has a short list of shapes a date can have, and
only those count. Anything else is just text.

### Regular expressions (regex)

A regex is a pattern for text. Some pieces used here:

| Piece | Means |
|---|---|
| `\d{1,2}` | one or two digits |
| `\s+` / `\s*` | one or more / zero or more spaces |
| `\b` | a word edge (so `mon` doesn't match inside `lemon`) |
| `(?:a\|b)` | `a` or `b` |
| `x?` | `x` is optional |
| `(?P<day>...)` | a **named group**: remember this part as `day` |
| `(?![a-z])` | "not followed by a letter" (so `oct` doesn't match `octopus`) |

The pieces are built once:

```python
MONTH = r"(?P<mon>" + _either(MONTHS) + r")(?![a-z])\.?"
DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
YEAR = r"(?:,?\s*(?P<year>\d{4}))?"
```

and joined into patterns, each paired with a function that turns the match into a date:

```python
PATTERNS = [
    # 25 oct, 25th october, 25 oct 2026
    (r"\b" + DAY + r"\s*(?:of\s+)?" + MONTH + YEAR + r"(?!\d)", _day_month),
    ...
    (r"\b(?P<after>day\s+after\s+)?(?:tomorrow|tomorow|tommorow|tommorrow|tmrw|tmrow|tmr|2morrow|2moro)\b", _tomorrow),
    ...
    # friday, this friday, next monday
    (r"\b(?:(?P<mod>this|next|coming|upcoming)\s+)?(?P<wd>" + LONG_WEEKDAYS + r")(?![a-z])", _weekday),
]
```

Why each false date is safe:

- `"chapter 5 notes"`, `"lab 3"`: a bare number never matches. A day number needs a
  month next to it (`5 nov`) or `on/by` + `th` (`by the 5th`).
- `"may i ..."`: `may` only counts with a day number next to it (`5 may`, `may 5`).
- `"pages 10-12"`, `"chapter 5.2"`: dashes and dots only count with a year
  (`25-10-2026`). Slashes (`25/10`) are allowed without one.
- `"SAT prep"`, `"sun chapter"`: `sat`/`sun` only count after `on/by/this/next`.
- `"tom and jerry"`: `tom` is a name, so it isn't on the "tomorrow" list.
- `"31 feb"`: matches the shape, but `date(2026, 2, 31)` raises `ValueError`, so it's
  thrown away.

### Turning a match into a date

```python
def _upcoming(today, month, day):
    """The next time this day/month happens (today counts)."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:  # e.g. 31 Feb
            continue
        if d >= today:
            return d
    return None
```

This is "prefer future": `"physics test 20 oct"` typed on 21 Oct means **next** year.
The Saved reply shows the year when it isn't this year, so you'd notice and tap Undo.

Weekdays use `%` (remainder) to count days forward:

```python
def _weekday(m, today):
    ahead = (WEEKDAYS[m["wd"]] - today.weekday()) % 7
    if ahead == 0 and m["mod"] == "next":
        ahead = 7  # "next friday" said on a Friday means a week later
    return today + timedelta(days=ahead)
```

Python counts Monday = 0 … Sunday = 6. On a Wednesday (2), "friday" (4) is
`(4 - 2) % 7 = 2` days ahead. "monday" (0) is `(0 - 2) % 7 = 5` days ahead.

### Picking one date and cleaning the task

```python
    # Deadlines are usually said last, so take the date that ends last.
    # If two end at the same spot, take the longer one ("day after tomorrow").
    start, end, due = max(found, key=lambda f: (f[1], -f[0]))

    before = text[:start]
    while LEFTOVER.search(before):
        before = LEFTOVER.sub("", before)
    return clean(before + " " + text[end:]), due
```

- We know where the date is (`start`, `end`), so we cut it out of the text.
- `LEFTOVER` removes little words left hanging before the date: "submit lab **by**",
  "assignment **due on**", "test **@**".
- `clean()` squashes double spaces and strips stray commas and dashes from the ends.

"Today" is always India's today, even though Vercel's clock runs on UTC:

```python
def india_today():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()
```

At 2 AM IST it's still yesterday in UTC, so without this, "tomorrow" would be wrong
for a few hours every night.

## 12. Testing the date finder

`test_dates.py` is a list of `(what you type, expected task, expected date)`:

```python
TODAY = date(2026, 10, 21)  # a Wednesday. Fixed, so the tests give the same answer every day.

CASES = [
    ("chem record friday",               "chem record",           date(2026, 10, 23)),
    ("pay fees next monday",             "pay fees",              date(2026, 10, 26)),
    ...
    ("chapter 5 notes",                  "chapter 5 notes",       None),
    ("may i borrow your notes",          "may i borrow your notes", None),
]
```

- `extract_date(text, today)` takes `today` as an argument instead of reading the
  clock. That's what makes it testable: the answer for "friday" depends on what day
  it is, so tests pin the day.
- Run it with `python test_dates.py`. When you find a sentence the bot gets wrong,
  add it here first, watch it fail, then fix `dates.py`.

## 13. RETURNING id and the Undo button

The Undo button needs to know *which row* to delete. Postgres can hand back the id
of the row it just made:

```python
    cur.execute("INSERT INTO tasks (chat_id, task, due, due_time) VALUES (%s, %s, %s, %s) RETURNING id",
                (chat_id, task, due, due_time))
    task_id = cur.fetchone()[0]
```

And `DELETE ... RETURNING task` tells us what we deleted (or nothing, if it was
already gone):

```python
    if action == "undo":
        # chat_id in the WHERE means you can only ever delete your own task.
        cur.execute("DELETE FROM tasks WHERE id = %s AND chat_id = %s RETURNING task", (task_id, chat_id))
        row = cur.fetchone()
        if row is None:
            return "Already removed", None, None
        return "Undone ↩️", f"↩️ Removed: {row[0]}", None
```

## 14. Ownership: chat_id in every WHERE

Ids are simple counting numbers (`SERIAL`): 1, 2, 3 … Your friend's task might be id
56 and yours 57. If the delete were only `WHERE id = %s`, anyone who could send
`done:56` would delete your friend's task.

So **every** query that reads or changes tasks also has `AND chat_id = %s`, using the
chat id **from Telegram's update**, never from the button data. If someone sends
`done:56` from their own chat, the query finds nothing and they get "Already done".

The old `/done` had this bug (`DELETE FROM tasks WHERE id = %s`). It was safe by luck
because the id came from a chat-filtered list, but now it's safe on purpose:

```python
            cur.execute("DELETE FROM tasks WHERE id = %s AND chat_id = %s", (real_id, chat_id))
```

## 15. Tap-to-finish lists and reminders

One function builds the list text **and** its buttons, so the same code runs for
`/list` and after every ✅ tap:

```python
    for number, (task_id, task, due, due_time) in enumerate(rows, start=1):
        line = f"{number}. {task}"
        ...
        if len(buttons) < 45:  # 2 buttons per row; Telegram allows 100 buttons per message
            # The buttons carry the real database id, not the list number.
            buttons.append([{"text": f"✅ {number}. {short(task)}", "callback_data": f"done:{task_id}"},
                            {"text": "✏️", "callback_data": f"edit:{task_id}"}])
```

Why the real id and not the list number? List numbers shift. If you tap ✅ on #2,
the old #3 becomes #2. A button made earlier saying "delete #3" would now delete the
wrong task. A database id never changes.

The morning reminder uses `rdone:` instead of `done:` so the tap redraws the
*reminder* (overdue/today/tomorrow) instead of turning it into the full list:

```python
    for chat_id, user_rows in per_user.items():
        text, buttons = reminder_view(user_rows, today)
        tg("sendMessage", chat_id=chat_id, text=text, reply_markup={"inline_keyboard": buttons})
```

## 16. ICS calendar feeds

An `.ics` file is plain text in a format every calendar app understands (its official
name is RFC 5545). `icsfeed.py` writes it by hand:

```python
    for task_id, task, due, due_time in rows:
        if due_time is None:
            # VALUE=DATE (no time) = an all-day event. DTEND is the day *after*.
            when = [f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
                    f"DTEND;VALUE=DATE:{due + timedelta(days=1):%Y%m%d}"]
        else:
            ...  # timed events: see section 20
        lines += [
            "BEGIN:VEVENT",
            # The UID never changes for a task, so the calendar updates it instead of making a copy.
            f"UID:task-{task_id}@deadline-buddy",
            f"DTSTAMP:{stamp}",
            *when,
            f"SUMMARY:{escape(task)}",
            "DESCRIPTION:From Deadline Buddy on Telegram",
            "TRANSP:TRANSPARENT",  # don't show you as "busy" all day
            "END:VEVENT",
        ]
```

- **All-day events** use a plain date (`20261025`) with no time and no time zone.
  That's right for deadlines: "due 25 Oct" is 25 Oct for you no matter what. The feed
  says `X-WR-TIMEZONE:Asia/Kolkata` so the calendar knows your home zone.
- **DTEND is exclusive**: an event on the 25th ends at the *start* of the 26th.
- **Stable UID**: Google remembers events by UID. Same task → same UID → it updates
  instead of making a duplicate. When you finish a task it disappears from the
  feed, and Google removes it on the next refresh.
- **Escaping**: `,` `;` and `\` mean something in `.ics`, so they get a backslash:

```python
def escape(text):
    # Commas, semicolons and backslashes mean something in .ics, so they get a backslash.
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
                .replace("\r", "").replace("\n", "\\n"))
```

- **Folding**: lines can be at most 75 *bytes*. Longer ones continue on the next line,
  starting with a space. An emoji is 4 bytes, so we count bytes, not letters:

```python
        n = len(ch.encode("utf-8"))  # emoji are 4 bytes, so count bytes, not letters
        if size + n > 75:
            parts.append(current)
            current, size = " ", 1
```

- Lines end with `\r\n` (CRLF), as the format requires.

**Subscribing vs importing:** "From URL" in Google Calendar *subscribes*: Google
re-downloads the link on its own every several hours (you can't make it faster). It's
read-only: editing the event in Google doesn't change the bot. That's why Phase 3
exists.

## 17. Secret links with random tokens

Google Calendar can't log in to anything. It just downloads a URL. So the URL itself
must be the secret:

```python
    # token_urlsafe(16) = 22 random letters/digits. Impossible to guess.
    new_token = secrets.token_urlsafe(16)
```

- `secrets` is Python's module for **security** randomness. Don't use `random` for
  this; its numbers can be predicted.
- 16 random bytes is 2¹²⁸ possibilities. Nobody is guessing that.
- We never put the chat_id in the URL. `/cal/123.ics` would let anyone read anyone's
  tasks by changing the number.

The first `/calendar` makes a token; later ones reuse it:

```python
        # Only sets a token if the user doesn't have one yet.
        cur.execute("UPDATE users SET cal_token = %s WHERE chat_id = %s AND cal_token IS NULL", (new_token, chat_id))
```

`/calendar reset` overwrites it, so the old link instantly returns 404. That's your
"I leaked my link" button.

## 18. Serving the feed from Flask

```python
@app.route("/cal/<token>.ics")
def calendar_feed(token):
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
```

- `<token>` in the route is a **URL variable**: Flask pulls it out and passes it to
  the function. `/cal/abc.ics` → `token = "abc"`.
- `Response(..., mimetype="text/calendar")` tells the client "this is a calendar", not
  a web page. Flask adds `charset=utf-8` for us, so emoji work.
- Unknown token → 404, the same answer as for a page that doesn't exist. That gives an
  attacker no hint about whether they were close.
- Tasks without a date are skipped (`due IS NOT NULL`): a calendar needs a day.

## 19. Validating the feed

Writing `.ics` by hand is easy to get slightly wrong, and Google won't tell you why it
ignored your feed. So `test_ics.py` reads our feed back with a real parser, the
`icalendar` package, and checks every event survived:

```python
    cal = Calendar.from_ical(text)
    events = cal.walk("VEVENT")
    assert len(events) == 3
    ...
    for event, (task_id, task, due) in zip(events, ROWS):
        assert str(event["UID"]) == f"task-{task_id}@deadline-buddy"
        assert str(event["SUMMARY"]) == task          # escaping/folding undone correctly
        assert event["DTSTART"].dt == due             # a date, not a datetime = all-day
```

The test tasks deliberately include commas, semicolons, a backslash and long emoji
names, the things most likely to break.

`icalendar` lives in **`requirements-dev.txt`**, not `requirements.txt`. Vercel installs
`requirements.txt`; the bot itself doesn't need `icalendar`, only the test does, so
there's no reason to make every deploy bigger.

```powershell
pip install -r requirements-dev.txt
python test_ics.py
```

You can also paste your production link into an online validator such as
icalendar.org's validator.

## 20. Times: "call mom friday 5pm"

### Finding the time

`dates.py` has three time patterns, used the same way as the date patterns:

```python
# "at 5pm", "by 11:59 pm", "5.30pm" (needs am/pm) ...
TIME_AMPM = re.compile(r"(?:\b(?:at|by|before|till|until)\s+)?\b(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>[ap])\.?m\b\.?", re.IGNORECASE)
# ... "17:30", "at 10:30" (24-hour, needs a colon so "chapter 5.30" stays normal) ...
TIME_24 = re.compile(r"(?:\b(?:at|by|before|till|until)\s+)?\b(?P<h>\d{1,2}):(?P<m>\d{2})(?![\d:])", re.IGNORECASE)
# ... "at noon", "midnight"
TIME_WORD = re.compile(r"(?:\b(?:at|by|before|till|until)\s+)?\b(?P<word>noon|midnight)\b", re.IGNORECASE)
```

- A time needs **am/pm** or a **colon**. So `5pm` and `17:30` are times, but
  `chapter 5.30` and `buy 5 apples` aren't.
- `at`/`by` in front is swallowed too, so "call mom **at 5pm**" leaves "call mom".

Then `_to_time` turns the match into a real time, and throws away impossible ones:

```python
    if groups.get("ampm"):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if groups["ampm"].lower() == "p" else 0)  # 12am = 0:00, 12pm = 12:00
    elif hour > 23:
        return None
```

The `% 12` trick: 5pm → `5 % 12 + 12 = 17`. 12pm → `12 % 12 + 12 = 12`. 12am →
`12 % 12 + 0 = 0` (midnight). So `13pm` and `25:00` are rejected and stay as normal text.

`extract_when` runs the time finder first, then the date finder on what's left:

```python
def extract_when(text, now):
    text, due_time = extract_time(text)
    task, due = extract_date(text, now.date())
    return task, due, due_time
```

### A time with no day

"call mom 5pm" has a time but no date. It means today, unless 5 PM already passed:

```python
def day_for_time(due_time, now):
    """A time without a date ("call mom 5pm") means today, or tomorrow if it already passed."""
    return now.date() if due_time > now.time() else now.date() + timedelta(days=1)
```

That's why `india_now()` exists next to `india_today()`: we need the clock, not just
the date.

### Storing and sorting

A new nullable column (safe: old tasks just get `NULL`, meaning "no time"):

```python
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due_time TIME")
```

One shared `ORDER BY` so `/done 3` and `/edit 3` always mean the 3rd row of `/list`:

```python
# Same order everywhere, so "/done 3" means the 3rd task in /list.
# Inside one day, all-day tasks come first, then timed ones by time.
TASK_ORDER = "ORDER BY due NULLS LAST, due_time NULLS FIRST, id"
```

### Timed calendar events

All-day events have no time zone, but a timed event does. 5 PM in India is not 5 PM in
London. The simplest correct way is to write the time in **UTC** (the `Z` at the end
means UTC), and every calendar app converts it back to your local time:

```python
def utc_stamp(due, due_time):
    """India date + time -> UTC text like 20261023T113000Z (5:00 PM IST = 11:30 UTC)."""
    start = datetime.combine(due, due_time, tzinfo=INDIA).astimezone(timezone.utc)
    return start.strftime("%Y%m%dT%H%M%SZ")
```

India is UTC+5:30, so 17:00 IST → 11:30 UTC. Timed events are made 1 hour long.
`test_ics.py` checks that the event really shows as 5:00 PM when converted back to
India time, and that an 11:30 PM event correctly ends the next day.

### What times don't do (yet)

The bot still only messages you **once a day**, at 5:30 AM. It can't ping you at 5 PM,
because Vercel Hobby cron jobs run at most once a day. Google Calendar usually doesn't
send alerts for events in URL-subscribed calendars either. Phase 3 (below) would fix
this for you: real events in your Google Calendar get Google's normal phone alerts.

## 21. Editing a task: remembering a conversation

Every webhook call is a fresh start. The code forgets everything after it answers. But
editing takes **two messages**: tap ✏️, then send the change. So the bot has to write
down "this person is editing task 57" somewhere that survives between requests: the
database.

```python
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS editing_task_id INTEGER")
```

This is called **conversation state**. Bot libraries have it built in; we do it by hand.

### Step 1: tap ✏️ → remember it

```python
def start_edit(cur, chat_id, task_id):
    """Remember which task this user is editing, and tell them what to send."""
    cur.execute("SELECT task, due, due_time FROM tasks WHERE id = %s AND chat_id = %s", (task_id, chat_id))
    row = cur.fetchone()
    if row is None:
        return None
    task, due, due_time = row
    cur.execute("UPDATE users SET editing_task_id = %s WHERE chat_id = %s", (task_id, chat_id))
```

The `AND chat_id = %s` check happens **before** saving the edit state, so you can't
start editing someone else's task.

A button tap can now also send a brand-new message (the instructions), so
`handle_callback` returns 4 things instead of 3:

```python
        return "Editing ✏️", None, None, instructions
```

### Step 2: the next message → apply it

`save_user` already runs on every message, so it also hands back the edit state, with no
extra query. `RETURNING` works on upserts too:

```python
        "ON CONFLICT (chat_id) DO UPDATE SET first_name = EXCLUDED.first_name "
        "RETURNING editing_task_id",
```

Then, at the very top of `handle_message`:

```python
    if editing_id is not None:
        # They tapped ✏️ before, so this message is the change. Editing ends either way.
        cur.execute("UPDATE users SET editing_task_id = NULL WHERE chat_id = %s", (chat_id,))
        if text == "/cancel":
            return "Okay, nothing changed 👍"
        if not text.startswith("/") and text not in [BTN_LIST, BTN_ADD, BTN_CAL, BTN_HELP]:
            return edit_task(cur, chat_id, editing_id, text)
        # A command or button: stop editing and do that instead (below).
```

The state is **always cleared**, whatever happens. A forgotten edit state is a nasty bug:
the next normal task you type would silently overwrite an old task.

### Merging the change

The user might send only a new day, only a new name, or both. `edit_task` keeps
whatever they didn't change:

```python
        task, due, due_time = extract_when(text, now)
        if not task:
            task = old_task  # they only sent a new day/time
        if due is None and due_time is None:
            due, due_time = old_due, old_time  # they only sent a new name
        elif due is None:
            due = old_due or day_for_time(due_time, now)  # only a new time: keep the day
```

| Task was | You send | Becomes |
|---|---|---|
| call mom — Fri, 5:00 PM | `7:30pm` | call mom — Fri, 7:30 PM |
| call mom — Fri, 7:30 PM | `call mom and dad` | call mom and dad — Fri, 7:30 PM |
| chem record — Thu | `chem record part 2 monday 9am` | chem record part 2 — Mon, 9:00 AM |
| buy notebook — Fri | `no date` | buy notebook (and it leaves Google Calendar) |

## 22. Phase 3: real Google Calendar sync, just for you (not built)

The plan: **only you** get two-way sync with your Google account. Everyone else keeps
the 📅 Calendar link exactly as now. **Nothing here is built yet.**

### How "only me" works

- A new environment variable, `OWNER_CHAT_ID`, holds your Telegram chat id.
- The `/connect` command only works when the message comes from that chat id. For
  anyone else it's treated like an unknown command. It isn't in `/help`, the keyboard
  or BotFather's command list, so your friends never see it.
- Since you're the only person who will ever sign in, the Google side gets much
  simpler (see "the limits" below).

### What it would do

1. You send `/connect`, sign in to Google once and press Allow (OAuth; see below).
2. The bot creates a separate **"Deadline Buddy"** calendar in your Google account.
   It only works inside that calendar and never touches your other events.
3. **Bot → Google, instantly:** adding, editing, ✅ or Undo immediately creates,
   changes or deletes the Google event. Timed tasks become real timed events, so
   **Google's normal phone alerts work** (a 5 PM notification for "call mom 5pm").
4. **Google → bot:** if you move, rename or delete an event in that calendar in Google,
   the bot catches up the **next time you message it**, and during the morning reminder.
   It asks Google "what changed since last time?" (Google gives you a *sync token* for
   exactly this). An event you create directly in that calendar becomes a task too.
5. You'd remove your 📅 URL subscription afterwards, so you don't see every task twice.

Why catch up "on your next message" instead of instantly? Google *can* push changes
to a webhook, but those push channels expire and need renewing, and Vercel Hobby cron
only runs once a day to renew them. Catching up when you open the bot is simpler, free,
and you only look at your tasks in the bot anyway.

### OAuth in one paragraph

The bot never sees your Google password. You sign in on Google's own page and press
Allow. Google sends you back to `https://deadline-buddy-one.vercel.app/oauth/callback`
with a one-time `code`. The bot swaps it for an **access token** (works ~1 hour) and a
**refresh token** (gets new access tokens later), and saves the refresh token in the
database. A random `state` value stops anyone from linking *their* Google account to
your chat.

### What you'd set up by hand in Google Cloud Console (free)

1. console.cloud.google.com → create a project ("Deadline Buddy").
2. **APIs & Services → Library** → enable **Google Calendar API**.
3. **OAuth consent screen** → **External** → app name, your email as support and
   developer email. Add a calendar scope. The bot needs to create its own calendar and
   manage events in it.
4. **Test users** → add your own Google email.
5. **Credentials → Create credentials → OAuth client ID** → **Web application** →
   authorized redirect URI: `https://deadline-buddy-one.vercel.app/oauth/callback`.
6. Put the client ID and secret in `.env` **and** in Vercel as `GOOGLE_CLIENT_ID` and
   `GOOGLE_CLIENT_SECRET`. Never commit them, never paste them in chat.
7. Optional but recommended: on the consent screen, press **Publish app** (switch from
   "Testing" to "In production"). See the next part for why.

### The limits, and why "only me" makes them small

- **Testing mode:** refresh tokens expire after **7 days**, so you'd need to `/connect`
  again every week. Publishing the app (step 7) stops that.
- **Published but unverified:** when you sign in, Google shows **"Google hasn't
  verified this app"**. Since it's your own app, you click **Advanced → Go to Deadline
  Buddy**. You see this once, when you connect.
- **100-user cap** for unverified apps: irrelevant, there's one user (you).
- **Verification** (privacy policy, owned domain, review): not needed for just you.

## 23. The webhook secret, explained

**The problem.** The bot's address `https://deadline-buddy-one.vercel.app/webhook` is
public. Right now, anything that arrives there is believed. But anyone can send a
message to a web address, not just Telegram. Someone who knew the address could send
a fake update that says "I am chat 12345, delete task 57", and the bot would do it,
because it can't tell a fake from the real Telegram.

**The fix: a shared password.** When you register your webhook with Telegram (the
`setWebhook` call), you can give it a `secret_token`. From then on, Telegram attaches
that word to every real update, in a header called `X-Telegram-Bot-Api-Secret-Token`.
The bot checks it:

```python
    # Only checked once WEBHOOK_SECRET is set, so the bot keeps working while it's being set up.
    # compare_digest compares safely (it takes the same time whether the guess is close or not).
    if WEBHOOK_SECRET:
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(sent, WEBHOOK_SECRET):
            return "Not allowed", 401
```

Why `hmac.compare_digest` instead of `!=`? A normal `!=` stops at the first wrong
letter, so a wrong guess that starts correctly takes a tiny bit longer to reject. An
attacker measuring those tiny delays could guess the secret one letter at a time.
`compare_digest` always takes the same time.

`fake_telegram.py` reads the same secret from `.env` and sends it in the header, just
like Telegram does, so local testing keeps working.

Fakers don't know the word, so their updates are thrown away. It's the same idea as
`CRON_SECRET` on `/cron/daily`, which you already have.

**Why the order matters.** It needs three things: the code check, the `WEBHOOK_SECRET`
env var in Vercel, and telling Telegram the word. If the code starts demanding the
word before Telegram sends it, every **real** message gets rejected and the bot goes
silent. Safe order:

1. Deploy code that checks the word **only if `WEBHOOK_SECRET` is set** (done).
2. Add `WEBHOOK_SECRET` in Vercel and redeploy.
3. Call `setWebhook` with the same `secret_token`.
4. Check `getWebhookInfo` shows no errors.

## 24. Ideas for later

- **A reminder at the exact time** ("call mom 5pm" → ping at 5 PM). Vercel Hobby cron
  is once a day, but a free outside service like cron-job.org could call a
  `/cron/soon` route every 5 minutes. (For you, Phase 3 gives this through Google's
  alerts.)
- **Repeating tasks** ("gym every monday").
- **Snooze** a reminder to tomorrow with one tap.
