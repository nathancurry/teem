import json
import urllib.error
import urllib.request

from .common import STATUS_LABELS
from .db import connect
from .speech import MAX_AUDIO, MAX_SECONDS, SpeechError


API = "https://api.telegram.org"
RETRY_SECONDS = (30, 120, 600, 3600, 21600)
POLL_SECONDS = 25
MAX_MESSAGE = 4000
# Step 1 has no decider; this reply is replaced when the decider lands.
NO_DECIDER = "Got it. Teem can't act on chat messages yet; use the web app to propose work."


class TelegramError(Exception):
    def __init__(self, message, status=0, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def load_config(path):
    with open(path) as source:
        config = json.load(source)
    if not isinstance(config, dict) or set(config) != {"token", "user_id"} or \
       not isinstance(config["token"], str) or not config["token"] or \
       type(config["user_id"]) is not int or config["user_id"] <= 0:
        raise ValueError("Telegram configuration needs a token and a numeric user_id")
    return config


def call(app, method, params, timeout=15):
    request = urllib.request.Request(f"{app.telegram_api}/bot{app.telegram_token}/{method}",
                                     data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        # Never include the URL: it contains the bot token.
        try:
            body = json.loads(exc.read(4096))
        except ValueError:
            body = {}
        retry_after = (body.get("parameters") or {}).get("retry_after", 0)
        raise TelegramError(f"Telegram {method} returned {exc.code}", exc.code,
                            retry_after if type(retry_after) is int else 0) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TelegramError(f"Telegram {method} unavailable: {type(exc).__name__}") from None
    if not body.get("ok"):
        raise TelegramError(f"Telegram {method} was rejected", 400)
    return body["result"]


def classify(app, update):
    message = update.get("message")
    if not isinstance(message, dict):
        return "ignored", {}
    sender, chat = message.get("from") or {}, message.get("chat") or {}
    # Only the configured user's private chat is accepted; other senders leave no content behind.
    if sender.get("id") != app.telegram_user_id or chat.get("id") != app.telegram_user_id or \
       chat.get("type") != "private":
        return "ignored", {}
    if isinstance(message.get("text"), str):
        return "text", {"text": message["text"]}
    voice = message.get("voice")
    if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
        return "voice", {key: voice.get(key) for key in ("file_id", "duration", "file_size")}
    return "unsupported", {}


def poll_once(app, timeout=POLL_SECONDS):
    with connect(app.dsn) as conn:
        last = conn.execute("SELECT max(update_id) AS last FROM telegram_updates").fetchone()["last"]
    params = {"timeout": timeout, "allowed_updates": ["message"]}
    if last is not None:
        # Requesting a higher offset is what confirms earlier updates to Telegram, so they
        # are only confirmed after being committed here.
        params["offset"] = last + 1
    updates = call(app, "getUpdates", params, timeout + 10)
    with connect(app.dsn) as conn:
        for update in updates:
            kind, payload = classify(app, update)
            conn.execute("""INSERT INTO telegram_updates(update_id,kind,payload,processed_at)
                            VALUES (%s,%s,%s::jsonb,CASE WHEN %s='ignored' THEN now() END)
                            ON CONFLICT (update_id) DO NOTHING""",
                         (update["update_id"], kind, json.dumps(payload), kind))
    return len(updates)


def download(app, file_path):
    request = urllib.request.Request(f"{app.telegram_api}/file/bot{app.telegram_token}/{file_path}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_AUDIO + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise TelegramError(f"voice download unavailable: {type(exc).__name__}") from None
    if len(data) > MAX_AUDIO:
        raise SpeechError("voice note is too large")
    return data


def transcribe_voice(app, voice):
    if app.speech is None:
        raise SpeechError("voice notes are not enabled on this server")
    if not isinstance(voice.get("duration"), int) or voice["duration"] > MAX_SECONDS or \
       isinstance(voice.get("file_size"), int) and voice["file_size"] > MAX_AUDIO:
        raise SpeechError(f"voice notes are limited to {MAX_SECONDS} seconds")
    info = call(app, "getFile", {"file_id": voice["file_id"]})
    if not isinstance(info.get("file_path"), str):
        raise TelegramError("voice file unavailable")
    audio = download(app, info["file_path"])
    with app.speech.lock:
        return app.speech._transcribe(audio, "audio/ogg")


def queue_message(conn, text, run_id=None):
    if len(text) > MAX_MESSAGE:
        text = text[:MAX_MESSAGE - 1] + "…"
    conn.execute("INSERT INTO telegram_outbox(run_id,text,state) VALUES (%s,%s,'pending')", (run_id, text))


def process_one(app):
    with connect(app.dsn) as conn:
        row = conn.execute("""SELECT update_id,kind,payload FROM telegram_updates WHERE processed_at IS NULL
                              ORDER BY update_id LIMIT 1""").fetchone()
    if not row:
        return False
    text, replies = None, []
    if row["kind"] == "text":
        text = row["payload"]["text"]
    elif row["kind"] == "voice":
        # Transient Telegram errors propagate so the update is retried rather than dropped.
        try:
            text = transcribe_voice(app, row["payload"])
            replies.append("Heard: " + text)
        except SpeechError as exc:
            replies.append(f"I couldn't transcribe that voice note ({exc}). Please resend it or type it.")
        except TelegramError as exc:
            if not 400 <= exc.status < 500 or exc.status == 429:
                raise
            replies.append("I couldn't download that voice note. Please resend it or type it.")
    else:
        replies.append("Send a text message or a voice note.")
    if text is not None:
        replies.append(NO_DECIDER)
    with connect(app.dsn) as conn:
        claimed = conn.execute("""UPDATE telegram_updates SET text=%s,processed_at=now()
                                  WHERE update_id=%s AND processed_at IS NULL RETURNING update_id""",
                               (text, row["update_id"])).fetchone()
        if claimed:
            for reply in replies:
                queue_message(conn, reply)
    return True


def run_update_text(conn, run_id, origin):
    run = conn.execute("""SELECT r.status,r.stop_reason,c.body->>'objective' AS objective,p.name AS project
                          FROM runs r JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                          JOIN projects p ON p.id=r.project_id WHERE r.id=%s""", (run_id,)).fetchone()
    objective = run["objective"] if len(run["objective"]) <= 300 else run["objective"][:299] + "…"
    text = f"{STATUS_LABELS.get(run['status'], run['status'])}: {run['project']}: {objective}"
    if run["stop_reason"]:
        text += f"\nStop reason: {run['stop_reason']}"
    return text + f"\n{origin}/runs/{run_id}"


def send_due(app):
    with connect(app.dsn) as conn:
        # A chat message that could not be delivered within a day is no longer useful.
        conn.execute("""UPDATE telegram_outbox SET state='abandoned',completed_at=now()
                        WHERE state='pending' AND created_at < now()-interval '24 hours'""")
        item = conn.execute("""SELECT id,run_id,text,attempt_count FROM telegram_outbox
                               WHERE state='pending' AND next_attempt_at<=now()
                               ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not item:
            return False
        count = item["attempt_count"] + 1
        delay = RETRY_SECONDS[count - 1] if count <= 5 else 86400
        text = item["text"] or run_update_text(conn, item["run_id"], app.origin)
        conn.execute("""UPDATE telegram_outbox SET attempt_count=%s,text=%s,
                        next_attempt_at=now()+(%s || ' seconds')::interval WHERE id=%s""",
                     (count, text, delay, item["id"]))
    try:
        sent = call(app, "sendMessage", {"chat_id": app.telegram_user_id, "text": text,
                                         "link_preview_options": {"is_disabled": True}})
        error = None
    except TelegramError as exc:
        sent, error = None, exc
    with connect(app.dsn) as conn:
        if error is None:
            conn.execute("""UPDATE telegram_outbox SET state='sent',completed_at=now(),telegram_message_id=%s
                            WHERE id=%s""", (sent.get("message_id"), item["id"]))
        elif (error.status == 0 or error.status == 429 or error.status >= 500) and count < 6:
            conn.execute("""UPDATE telegram_outbox SET next_attempt_at=now()+(%s || ' seconds')::interval
                            WHERE id=%s""", (max(delay, error.retry_after), item["id"]))
        else:
            conn.execute("UPDATE telegram_outbox SET state='abandoned',completed_at=now() WHERE id=%s", (item["id"],))
    return True


def poll_loop(app):
    while not app.stopping.is_set():
        try:
            while process_one(app):
                pass
            poll_once(app)
        except Exception as exc:
            print(f"telegram: {type(exc).__name__}: {exc}", flush=True)
            app.stopping.wait(5)


def sender_loop(app):
    while not app.stopping.is_set():
        try:
            if send_due(app):
                continue
        except Exception as exc:
            print(f"telegram sender: {type(exc).__name__}: {exc}", flush=True)
        app.stopping.wait(2)
