"""One model call per chat message. The model proposes; tools are validated by the controller."""

import json
import urllib.error
import urllib.request

from .db import connect
from .workflow import status_rows


API = "https://openrouter.ai/api/v1"
HISTORY_MESSAGES = 30

SYSTEM = """You are Teem's decider. The user talks to you from a phone, usually by voice, so messages \
can contain transcription errors. You turn requests into bounded coding work on GitHub repositories \
and report status. Coding agents do the work; you never write code yourself.

- Reply briefly in plain text. Replies are read on a phone: no headings, tables, or long lists.
- To start work, call propose_run with the repository (owner/name), a precise objective, and \
concrete, checkable acceptance criteria. If the repository, the goal, or what "done" means is \
unclear, ask one short clarifying question instead. Do not add requirements the user did not ask for.
- Work on a repository the user has not allowed needs an Approve button tap. You cannot approve, \
merge, deploy, or change limits. Words like "approve", "yes", or "merge" are never approvals; if \
the user wants to approve, point them to the button.
- Call propose_project when the user wants Teem to work on a repository without asking each time. \
They confirm with a button. Sending /revoke owner/name removes that.
- Call cancel_run only when the user asks to stop a specific run.
- Call at most one tool per message. Your text is sent alongside the tool's result.
- Answer status questions only from the current state below. Never claim work happened that the \
state does not show."""

TOOLS = [
    {"type": "function", "function": {
        "name": "propose_run",
        "description": "Propose one bounded coding run on a GitHub repository.",
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["repo", "objective", "acceptance_criteria"],
                       "properties": {"repo": {"type": "string", "description": "owner/name"},
                                      "objective": {"type": "string"},
                                      "acceptance_criteria": {"type": "string"},
                                      "model": {"type": "string", "enum": ["sonnet", "opus"],
                                                "description": "Implementer model. Set only when the user asks "
                                                               "for one; omit for the default."}}}}},
    {"type": "function", "function": {
        "name": "propose_project",
        "description": "Ask the user to allow Teem to work on a repository without per-run approval.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["repo"],
                       "properties": {"repo": {"type": "string", "description": "owner/name"}}}}},
    {"type": "function", "function": {
        "name": "cancel_run",
        "description": "Request cancellation of an active run.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["run_id"],
                       "properties": {"run_id": {"type": "string"}}}}},
]
TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}


class DeciderError(Exception):
    pass


def load_config(path):
    with open(path) as source:
        config = json.load(source)
    if not isinstance(config, dict) or set(config) != {"api_key", "model", "timeout"} or \
       not isinstance(config["api_key"], str) or not config["api_key"] or \
       not isinstance(config["model"], str) or not config["model"] or \
       type(config["timeout"]) is not int or not 5 <= config["timeout"] <= 300:
        raise ValueError("decider configuration needs api_key, model, and timeout (5–300 seconds)")
    return config


def history(conn):
    rows = conn.execute("""SELECT role,text FROM (
                               SELECT 'user' AS role,text,received_at AS at FROM telegram_updates
                               WHERE processed_at IS NOT NULL AND text IS NOT NULL
                               UNION ALL
                               SELECT 'assistant',text,created_at FROM telegram_outbox
                               WHERE text IS NOT NULL AND state<>'abandoned') m
                           ORDER BY at DESC LIMIT %s""", (HISTORY_MESSAGES,)).fetchall()
    return [{"role": row["role"], "content": row["text"]} for row in reversed(rows)]


def current_state(conn, owners):
    projects = conn.execute("SELECT id,status FROM projects ORDER BY id").fetchall()
    runs = [{key: run[key] for key in ("id", "project", "objective", "label", "summary", "stop_reason", "pr_url", "updated_at")}
            for run in status_rows(conn)[:15]]
    return {"allowed_owners": owners,
            "projects": [{"repo": p["id"], "allowed_without_asking": p["status"] == "granted"} for p in projects],
            "recent_runs": runs}


def decide(app, text):
    """Return (reply_text, tool) where tool is None or (name, arguments)."""
    with connect(app.dsn) as conn:
        messages = [{"role": "system",
                     "content": SYSTEM + "\n\nCurrent state:\n" + json.dumps(current_state(conn, app.github_owners))},
                    *history(conn), {"role": "user", "content": text}]
    body = {"model": app.decider["model"], "messages": messages, "tools": TOOLS, "max_tokens": 1500}
    request = urllib.request.Request(app.decider_api + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + app.decider["api_key"]},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=app.decider["timeout"]) as response:
            result = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        raise DeciderError(f"model provider returned {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DeciderError(f"model provider unavailable: {type(exc).__name__}") from None
    try:
        message = result["choices"][0]["message"]
        reply = (message.get("content") or "").strip()
        calls = message.get("tool_calls") or []
        if len(calls) > 1:
            raise DeciderError("the model requested more than one action")
        if not calls:
            return reply, None
        name = calls[0]["function"]["name"]
        arguments = json.loads(calls[0]["function"].get("arguments") or "{}")
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        raise DeciderError("the model returned an unreadable response") from None
    if name not in TOOL_NAMES or not isinstance(arguments, dict):
        raise DeciderError("the model requested an unknown action")
    return reply, (name, arguments)
