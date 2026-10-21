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
20. [Phase 3: real Google Calendar sync (not built)](#20-phase-3-real-google-calendar-sync-not-built)
21. [Ideas for later](#21-ideas-for-later)

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
    return reply, [[{"text": "↩️ Undo", "callback_data": f"undo:{task_id}"}]]
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
    cur.execute("INSERT INTO tasks (chat_id, task, due) VALUES (%s, %s, %s) RETURNING id", (chat_id, task, due))
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
    for number, (task_id, task, due) in enumerate(rows, start=1):
        line = f"{number}. {task}"
        ...
        if len(buttons) < 50:  # Telegram allows 100 buttons per message
            # The button carries the real database id, not the list number.
            buttons.append([{"text": f"✅ {number}. {short(task)}", "callback_data": f"done:{task_id}"}])
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
    for task_id, task, due in rows:
        lines += [
            "BEGIN:VEVENT",
            # The UID never changes for a task, so the calendar updates it instead of making a copy.
            f"UID:task-{task_id}@deadline-buddy",
            f"DTSTAMP:{stamp}",
            # VALUE=DATE (no time) = an all-day event. DTEND is the day *after*.
            f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
            f"DTEND;VALUE=DATE:{due + timedelta(days=1):%Y%m%d}",
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
        cur.execute("SELECT id, task, due FROM tasks WHERE chat_id = %s AND due IS NOT NULL ORDER BY due, id", (user[0],))
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

## 20. Phase 3: real Google Calendar sync (not built)

The ICS feed is one-way and slow (Google refreshes every few hours). Real two-way
sync means the bot writes straight into your Google Calendar, and changes in Google
come back. **None of this is built.** Say "go phase 3" if you want it.

### What it needs: OAuth 2.0

The bot can't use your Google password. Instead, OAuth works like this:

1. You send `/connect`. The bot replies with a Google sign-in link.
2. You sign in **on Google's site** and click "Allow Deadline Buddy to manage your
   calendar events".
3. Google sends your browser back to `https://deadline-buddy-one.vercel.app/oauth/callback?code=...&state=...`.
4. The bot swaps that `code` for two tokens: an **access token** (works for about
   1 hour) and a **refresh token** (used to get new access tokens later).
5. The bot saves the refresh token in the database (a new table, encrypted). After
   that, `/add` and ✅ call the Google Calendar API to create and delete events.

New code: a `/connect` command, an `/oauth/callback` route, a `google_accounts`
table, calls to the Calendar API (`events.insert`, `events.delete`), a `/disconnect`
command that revokes the token, and a signed `state` value so nobody can link *their*
Google account to *your* chat. All free: the Calendar API costs nothing at this size.

### What you'd set up by hand in Google Cloud Console

1. Go to console.cloud.google.com and create a project ("Deadline Buddy").
2. **APIs & Services → Library** → enable **Google Calendar API**.
3. **OAuth consent screen** → user type **External** → app name, support email,
   developer email. Add the scope `.../auth/calendar.events`.
4. **Test users** → add your Google email and each friend's Google email.
5. **Credentials → Create credentials → OAuth client ID** → type **Web application** →
   authorized redirect URI `https://deadline-buddy-one.vercel.app/oauth/callback`.
6. Copy the client ID and client secret into Vercel as `GOOGLE_CLIENT_ID` and
   `GOOGLE_CLIENT_SECRET` (and into `.env`). Never commit them.

### The limits (important for a free student project)

- **Testing mode:** only the emails you list as test users can sign in, and the
  **refresh tokens expire after 7 days**. Everyone would have to `/connect` again
  every week.
- **Publishing without verification:** refresh tokens stop expiring, but users see
  a scary **"Google hasn't verified this app"** screen (they click Advanced → Go to
  Deadline Buddy). Apps using sensitive scopes like calendar are capped at
  **100 users** until verified.
- **Verification** is free but slow. Google wants a privacy policy page, a home page,
  a verified domain you own (a `vercel.app` address isn't yours), and sometimes a
  demo video. It can take weeks.
- **Google → bot direction:** to notice edits made in Google, you'd either use push
  notifications (`events.watch`, which sends to a webhook and has to be renewed before
  it expires) or check with a cron job. Vercel Hobby cron jobs run at most **once a
  day**, so the cron way would be slow.

For a group of friends, the ICS feed you have now gets most of the benefit with none
of this hassle.

## 21. Ideas for later

- **Webhook secret.** Right now anyone who knows your webhook URL could POST a fake
  update. Telegram can send a secret header with every update: call `setWebhook` with
  `secret_token=<random string>`, store the same string as `WEBHOOK_SECRET` in Vercel,
  and reject requests where `request.headers.get("X-Telegram-Bot-Api-Secret-Token")`
  doesn't match.
- **Times, not just dates.** "call mom tomorrow 5pm" keeps "5pm" in the task text. A
  `due_time` column plus timed calendar events would use it.
- **Edit a task** (change its date) with another inline button.
