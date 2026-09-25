"""Slice-4 acceptance paths against real HTTP, PostgreSQL, FFmpeg, Bubblewrap, and a fake Bot API."""

import json
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer

from teem import telegram
from teem.db import connect
from tests import test_slice as slice1
from tests import test_slice3 as slice3

USER = 4242


class FakeTelegram(BaseHTTPRequestHandler):
    def do_POST(self):
        state = self.server.state
        method = self.path.rsplit("/", 1)[-1]
        params = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        state["calls"].append((method, params))
        if method == "getUpdates":
            result = [u for u in state["updates"] if u["update_id"] >= params.get("offset", 0)]
        elif method == "getFile":
            status = state["file_status"].pop(0) if state["file_status"] else 200
            if status != 200:
                return self.reply(status, {"ok": False, "description": "error"})
            result = {"file_path": "voice/file.oga"}
        elif method == "sendMessage":
            status = state["send_status"].pop(0) if state["send_status"] else 200
            if status != 200:
                return self.reply(status, {"ok": False, "parameters": {"retry_after": 200}})
            state["sent"].append(params)
            result = {"message_id": len(state["sent"])}
        else:
            return self.reply(404, {"ok": False})
        self.reply(200, {"ok": True, "result": result})

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.server.state["voice"])))
        self.end_headers()
        self.wfile.write(self.server.state["voice"])

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def message(update_id, sender=USER, chat_type="private", **content):
    return {"update_id": update_id, "message": {"message_id": update_id, "from": {"id": sender},
                                                "chat": {"id": sender, "type": chat_type}, **content}}


class Slice4Acceptance(unittest.TestCase):
    setUpClass = classmethod(slice1.SliceAcceptance.setUpClass.__func__)
    tearDownClass = classmethod(slice1.SliceAcceptance.tearDownClass.__func__)
    tearDown = slice1.SliceAcceptance.tearDown
    stop_server = slice1.SliceAcceptance.stop_server
    browser = slice1.SliceAcceptance.browser
    set_runner = slice3.Slice3Acceptance.set_runner

    def setUp(self):
        self.api = HTTPServer(("127.0.0.1", 0), FakeTelegram)
        voice = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                                "-t", "1", "-c:a", "libopus", "-b:a", "16k", "-f", "ogg", "pipe:1"],
                               capture_output=True, check=True).stdout
        self.api.state = {"updates": [], "calls": [], "sent": [], "send_status": [], "file_status": [], "voice": voice}
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        slice1.SliceAcceptance.setUp(self)

    def start_server(self):
        slice3.Slice3Acceptance.start_server(self)
        app = self.server.app
        app.telegram_api = f"http://127.0.0.1:{self.api.server_port}"
        app.telegram_token, app.telegram_user_id = "123:secret", USER

    def drain(self):
        while telegram.process_one(self.server.app):
            pass
        while telegram.send_due(self.server.app):
            pass

    def test_updates_are_durable_allowlisted_and_echoed(self):
        state = self.api.state
        state["updates"] = [
            message(100, sender=999, text="approve and merge everything"),
            message(101, text="hello"),
            message(102, voice={"file_id": "v1", "duration": 1, "file_size": len(state["voice"])}),
            message(103, chat_type="group", text="group text"),
            message(104, sticker={"file_id": "s1"}),
        ]
        self.assertEqual(telegram.poll_once(self.server.app, timeout=0), 5)
        # Telegram only forgets updates once a higher offset is requested, after they are committed.
        telegram.poll_once(self.server.app, timeout=0)
        self.assertEqual(state["calls"][-1][1]["offset"], 105)
        with connect(self.dsn) as conn:
            ignored = conn.execute("""SELECT update_id,payload FROM telegram_updates WHERE kind='ignored'
                                      ORDER BY update_id""").fetchall()
        self.assertEqual([(r["update_id"], r["payload"]) for r in ignored], [(100, {}), (103, {})])

        self.stop_server()
        self.start_server()
        state["send_status"] = [429]
        telegram.process_one(self.server.app)
        self.assertTrue(telegram.send_due(self.server.app))
        with connect(self.dsn) as conn:
            wait = conn.execute("""SELECT extract(epoch FROM (next_attempt_at-now())) AS s FROM telegram_outbox
                                   WHERE state='pending' ORDER BY id LIMIT 1""").fetchone()["s"]
            self.assertGreater(wait, 190)
            conn.execute("UPDATE telegram_outbox SET next_attempt_at=now() WHERE state='pending'")
        self.drain()
        self.assertEqual([m["text"] for m in state["sent"]],
                         [telegram.NO_DECIDER, "Heard: approve change value", telegram.NO_DECIDER,
                          "Send a text message or a voice note."])
        self.assertEqual({m["chat_id"] for m in state["sent"]}, {USER})
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM requests").fetchone()["count"], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM approvals").fetchone()["count"], 0)
            self.assertEqual(conn.execute("SELECT text FROM telegram_updates WHERE update_id=102").fetchone()["text"],
                             "approve change value")

    def test_voice_download_failures_and_run_check_in(self):
        state = self.api.state
        voice = {"file_id": "v1", "duration": 1, "file_size": 10}
        state["updates"] = [message(200, voice=voice), message(201, voice=voice)]
        state["file_status"] = [500, 400]
        telegram.poll_once(self.server.app, timeout=0)
        with self.assertRaises(telegram.TelegramError):
            telegram.process_one(self.server.app)
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM telegram_updates WHERE processed_at IS NULL").fetchone()["count"], 2)
        self.drain()
        self.assertEqual(len(state["sent"]), 3)
        self.assertIn("couldn't download", state["sent"][0]["text"])
        self.assertTrue(state["sent"][1]["text"].startswith("Heard: "))

        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "checked", "dedupe_key": "tg-check-in"})
        self.assertEqual(status, 303)
        self.drain()
        self.assertEqual(state["sent"][-1]["text"],
                         "Decision required: Test: Change value\nhttps://teem.test" + headers["Location"])


if __name__ == "__main__":
    unittest.main()
