"""An OpenAI-compatible chat endpoint, so voice apps can talk to the decider.

The app does speech recognition and speech synthesis; Teem receives and returns text. Each request
is one decider turn in the same conversation as Telegram, with the same tools and limits. Nothing
said here can approve work: approval requests and their buttons go to Telegram as usual.
"""

import json
import re
import time
import uuid

from .db import connect
from .telegram import NO_DECIDER, apply_action, prepare

MODEL = "teem"
URL = re.compile(r"\s*https?://\S+")


def latest_user_text(body):
    """The newest user message. The app's own history and system prompt are ignored: Teem keeps the
    conversation itself, so it stays the same one across Telegram and voice."""
    messages = body.get("messages") if isinstance(body, dict) else None
    for message in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content
                               if isinstance(part, dict) and part.get("type") == "text")
        if isinstance(content, str) and content.strip():
            return content.strip()[:4000]
    return None


def turn(app, text):
    """Run one decider turn for a spoken message and return the reply to speak."""
    if app.decider is None:
        replies, action = [NO_DECIDER], None
    else:
        replies, action = prepare(app, text, spoken=True)
    with connect(app.dsn) as conn:
        turn_id = conn.execute("INSERT INTO chat_turns(role,text) VALUES ('user',%s) RETURNING id",
                               (text,)).fetchone()["id"]
        if action:
            replies += apply_action(conn, app, f"chat:{turn_id}", action, spoken=True)
        reply = URL.sub("", " ".join(replies)).strip() or "Done."
        conn.execute("INSERT INTO chat_turns(role,text) VALUES ('assistant',%s)", (reply,))
    return reply


def completion(reply):
    return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
            "model": MODEL, "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def stream_events(reply):
    """The reply as server-sent events: one content chunk, a finish chunk, then [DONE]."""
    base = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion.chunk",
            "created": int(time.time()), "model": MODEL}
    chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply},
                                    "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "created": 0, "owned_by": "teem"}]}
