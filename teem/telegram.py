import json
import urllib.error
import urllib.request

import psycopg

from . import decider, github
from .common import STATUS_LABELS, ApiError
from .db import connect
from .speech import MAX_AUDIO, MAX_SECONDS, SpeechError
from .workflow import cancel_run, create_run, decide_run


API = "https://api.telegram.org"
RETRY_SECONDS = (30, 120, 600, 3600, 21600)
POLL_SECONDS = 25
MAX_MESSAGE = 4000
NO_DECIDER = "Got it. The decider isn't configured on this server, so chat messages can't start work."


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
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        chat = ((callback.get("message") or {}).get("chat") or {})
        if (callback.get("from") or {}).get("id") != app.telegram_user_id or \
           chat.get("id") != app.telegram_user_id or not isinstance(callback.get("data"), str):
            return "ignored", {}
        return "callback", {"id": callback.get("id"), "data": callback["data"]}
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
    params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
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


def prepare(app, text):
    """Ask the decider and do any network work its action needs, outside a transaction.

    Returns (replies, action) where action is None or a dict applied by apply_action.
    """
    try:
        reply, tool = decider.decide(app, text)
    except decider.DeciderError as exc:
        return [f"The decider is unavailable ({exc}). Please try again."], None
    replies = [reply] if reply else []
    if not tool:
        return replies, None
    name, args = tool
    if name == "cancel_run":
        return replies, {"kind": name, "run_id": str(args.get("run_id", ""))}
    repo = github.normalize_repo(args.get("repo"), app.github_owners)
    if not repo:
        return replies + ["I can only work on GitHub repositories owned by " + ", ".join(app.github_owners) + "."], None
    if name == "propose_project":
        return replies, {"kind": name, "repo": repo}
    objective, criteria = args.get("objective"), args.get("acceptance_criteria")
    if not isinstance(objective, str) or not isinstance(criteria, str) or not objective.strip() or not criteria.strip():
        return replies + ["That proposal was missing an objective or acceptance criteria."], None
    try:
        base, checks = github.fetch_base(app, repo)
    except github.GitHubError as exc:
        return replies + [f"I couldn't read {repo} from GitHub: {exc}"], None
    return replies, {"kind": name, "repo": repo, "base": base, "checks": checks,
                     "objective": objective.strip(), "criteria": criteria.strip()}


def ensure_project(conn, repo):
    conn.execute("""INSERT INTO projects(id,name,status) VALUES (%s,%s,'proposed')
                    ON CONFLICT (id) DO NOTHING""", (repo, repo))
    return conn.execute("SELECT * FROM projects WHERE id=%s FOR UPDATE", (repo,)).fetchone()


def apply_action(conn, app, update_id, action):
    """Apply a validated action inside the update's processing transaction. Returns replies."""
    kind = action["kind"]
    if kind == "revoke":
        repo = github.normalize_repo(action["repo"], app.github_owners)
        revoked = repo and conn.execute("""UPDATE projects SET status='revoked',decided_at=now()
                                           WHERE id=%s AND status='granted' RETURNING id""", (repo,)).fetchone()
        if not revoked:
            return [f"{action['repo'] or 'That repository'} was not allowed."]
        return [f"Revoked. Teem will ask before each run on {repo}."]
    if kind == "propose_project":
        project = ensure_project(conn, action["repo"])
        if project["status"] == "granted":
            return [f"{project['id']} is already allowed."]
        # A fresh grant_id makes any earlier Allow button for this repository stale.
        conn.execute("""UPDATE projects SET status='proposed',grant_id=gen_random_uuid(),decided_at=NULL
                        WHERE id=%s""", (project["id"],))
        conn.execute("INSERT INTO telegram_outbox(project_id,state) VALUES (%s,'pending')", (project["id"],))
        return []
    if kind == "propose_run":
        ensure_project(conn, action["repo"])
        try:
            with conn.transaction():
                run_id, _ = create_run(conn, action["repo"], action["base"], action["checks"], app.reviewer,
                                       action["objective"], action["criteria"], f"tg:{update_id}")
        except psycopg.errors.UniqueViolation:
            return [f"A run is already active on {action['repo']}."]
        except ApiError as exc:
            return [f"I couldn't create that run: {exc.message}."]
        status = conn.execute("SELECT status FROM runs WHERE id=%s", (run_id,)).fetchone()["status"]
        return [f"Started on {action['repo']}: {action['objective']}"] if status == "queued" else []
    if kind == "cancel_run":
        try:
            with conn.transaction():
                cancel_run(conn, action["run_id"])
        except ApiError as exc:
            return [f"I couldn't cancel that run: {exc.message}."]
        except psycopg.errors.InvalidTextRepresentation:
            return ["I couldn't find that run."]
        return ["Cancellation requested."]
    raise ValueError("unknown action")


def apply_callback(conn, data):
    parts = data.split(":")
    try:
        with conn.transaction():
            if parts[0] == "r" and len(parts) == 4 and parts[3] in ("a", "d"):
                decision = "approve" if parts[3] == "a" else "deny"
                decide_run(conn, parts[1], parts[2], decision, "telegram_button")
                return ["Approved. The run is queued." if decision == "approve" else "Denied."]
            if parts[0] == "p" and len(parts) == 3 and parts[2] in ("a", "d"):
                project = conn.execute("SELECT * FROM projects WHERE grant_id=%s FOR UPDATE", (parts[1],)).fetchone()
                if not project or project["status"] != "proposed":
                    return ["That request no longer applies."]
                status = "granted" if parts[2] == "a" else "denied"
                conn.execute("UPDATE projects SET status=%s,decided_at=now() WHERE id=%s", (status, project["id"]))
                if status == "denied":
                    return [f"Denied. Teem will ask before each run on {project['id']}."]
                return [f"Allowed. Teem can now start runs on {project['id']} without asking. Merging still needs you. "
                        f"Send /revoke {project['id']} to undo."]
    except ApiError as exc:
        return [f"That decision no longer applies ({exc.message})."]
    except psycopg.errors.InvalidTextRepresentation:
        pass
    return ["That button is not valid."]


def process_one(app):
    with connect(app.dsn) as conn:
        row = conn.execute("""SELECT update_id,kind,payload FROM telegram_updates WHERE processed_at IS NULL
                              ORDER BY update_id LIMIT 1""").fetchone()
    if not row:
        return False
    if row["kind"] == "callback":
        with connect(app.dsn) as conn:
            if conn.execute("""UPDATE telegram_updates SET processed_at=now() WHERE update_id=%s
                               AND processed_at IS NULL RETURNING update_id""", (row["update_id"],)).fetchone():
                for reply in apply_callback(conn, row["payload"]["data"]):
                    queue_message(conn, reply)
        try:
            call(app, "answerCallbackQuery", {"callback_query_id": row["payload"]["id"]})
        except TelegramError:
            pass
        return True
    text, replies, action = None, [], None
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
    # Commands are only honored when typed, never from a transcript.
    if row["kind"] == "text" and text.startswith("/revoke"):
        action = {"kind": "revoke", "repo": text[len("/revoke"):].strip()}
    elif text is not None and app.decider is None:
        replies.append(NO_DECIDER)
    elif text is not None:
        decided, action = prepare(app, text)
        replies += decided
    with connect(app.dsn) as conn:
        claimed = conn.execute("""UPDATE telegram_updates SET text=%s,processed_at=now()
                                  WHERE update_id=%s AND processed_at IS NULL RETURNING update_id""",
                               (text, row["update_id"])).fetchone()
        if claimed:
            # The echo and decider reply go first; the action may queue its own check-in.
            for reply in replies:
                queue_message(conn, reply)
            if action:
                for reply in apply_action(conn, app, row["update_id"], action):
                    queue_message(conn, reply)
    return True


def run_update(conn, run_id, origin):
    run = conn.execute("""SELECT r.status,r.stop_reason,r.contract_version,c.body,p.name AS project
                          FROM runs r JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                          JOIN projects p ON p.id=r.project_id WHERE r.id=%s""", (run_id,)).fetchone()
    contract = run["body"]
    objective = contract["objective"] if len(contract["objective"]) <= 300 else contract["objective"][:299] + "…"
    text = f"{STATUS_LABELS.get(run['status'], run['status'])}: {run['project']}: {objective}"
    if run["stop_reason"]:
        text += f"\nStop reason: {run['stop_reason']}"
    buttons = None
    if run["status"] == "awaiting_approval":
        checks = ", ".join(check["name"] for check in contract["checks"]) or "none (.teem/checks.json not found)"
        text += (f"\nDone when: {contract['acceptance_criteria'][:600]}\nChecks: {checks}"
                 f"\nBase: {contract['base_commit'][:10]} · up to {contract['limits']['revisions']} revisions")
        prefix = f"r:{run_id}:{run['contract_version']}:"
        buttons = [[{"text": "Approve", "callback_data": prefix + "a"}, {"text": "Deny", "callback_data": prefix + "d"}]]
    return text + f"\n{origin}/runs/{run_id}", buttons


def project_prompt(conn, project_id):
    project = conn.execute("SELECT status,grant_id FROM projects WHERE id=%s", (project_id,)).fetchone()
    if project["status"] != "proposed":
        return f"{project_id} is {project['status']}.", None
    text = (f"Allow Teem to start runs on {project_id} without asking each time? It may push teem/* branches "
            "and open pull requests there. Merging stays with you.")
    prefix = f"p:{project['grant_id']}:"
    return text, [[{"text": "Allow", "callback_data": prefix + "a"}, {"text": "Deny", "callback_data": prefix + "d"}]]


def send_due(app):
    with connect(app.dsn) as conn:
        # A chat message that could not be delivered within a day is no longer useful.
        conn.execute("""UPDATE telegram_outbox SET state='abandoned',completed_at=now()
                        WHERE state='pending' AND created_at < now()-interval '24 hours'""")
        item = conn.execute("""SELECT id,run_id,project_id,text,attempt_count FROM telegram_outbox
                               WHERE state='pending' AND next_attempt_at<=now()
                               ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not item:
            return False
        count = item["attempt_count"] + 1
        delay = RETRY_SECONDS[count - 1] if count <= 5 else 86400
        # Run and project messages are composed from current state, so a late message never
        # offers a button for a decision that has already been made.
        buttons = None
        if item["text"]:
            text = item["text"]
        elif item["run_id"]:
            text, buttons = run_update(conn, item["run_id"], app.origin)
        else:
            text, buttons = project_prompt(conn, item["project_id"])
        conn.execute("""UPDATE telegram_outbox SET attempt_count=%s,text=%s,
                        next_attempt_at=now()+(%s || ' seconds')::interval WHERE id=%s""",
                     (count, text, delay, item["id"]))
    try:
        params = {"chat_id": app.telegram_user_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        sent = call(app, "sendMessage", params)
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
