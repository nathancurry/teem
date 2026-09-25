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


class FakeModel(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        content, tool = self.server.responses.pop(0)
        message = {"role": "assistant", "content": content}
        if tool:
            message["tool_calls"] = [{"id": "call", "type": "function",
                                      "function": {"name": tool[0], "arguments": json.dumps(tool[1])}}]
        data = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def callback(update_id, data, sender=USER):
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "from": {"id": sender},
                                                       "message": {"chat": {"id": sender}}, "data": data}}


class Slice4Acceptance(unittest.TestCase):
    setUpClass = classmethod(slice1.SliceAcceptance.setUpClass.__func__)
    tearDownClass = classmethod(slice1.SliceAcceptance.tearDownClass.__func__)
    tearDown = slice1.SliceAcceptance.tearDown
    stop_server = slice1.SliceAcceptance.stop_server
    browser = slice1.SliceAcceptance.browser
    claim_and_execute = slice1.SliceAcceptance.claim_and_execute
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
        self.model = HTTPServer(("127.0.0.1", 0), FakeModel)
        self.model.requests, self.model.responses = [], []
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(self.model.shutdown)
        self.use_decider = False
        slice1.SliceAcceptance.setUp(self)
        with connect(self.dsn) as conn:
            conn.execute("DELETE FROM telegram_outbox")
            conn.execute("DELETE FROM telegram_updates")

    def start_server(self):
        slice3.Slice3Acceptance.start_server(self)
        app = self.server.app
        app.telegram_api = f"http://127.0.0.1:{self.api.server_port}"
        app.telegram_token, app.telegram_user_id = "123:secret", USER
        if self.use_decider:
            app.decider_api = f"http://127.0.0.1:{self.model.server_port}"
            app.decider = {"api_key": "key", "model": "test-model", "timeout": 10}

    def enable_decider(self):
        self.use_decider = True
        self.stop_server()
        self.start_server()
        self.worker.api.url = self.url

    def deliver(self, *updates):
        self.api.state["updates"] = list(updates)
        telegram.poll_once(self.server.app, timeout=0)
        self.drain()

    def texts(self):
        return [m["text"] for m in self.api.state["sent"]]

    def run_row(self):
        with connect(self.dsn) as conn:
            return conn.execute("SELECT * FROM runs WHERE project_id=%s ORDER BY created_at DESC LIMIT 1",
                                (self.project_id,)).fetchone()

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
            self.assertEqual(conn.execute("SELECT count(*) FROM runs WHERE project_id=%s", (self.project_id,)).fetchone()["count"], 0)
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
        check_in = state["sent"][-1]
        self.assertTrue(check_in["text"].startswith("Decision required: Test: Change value\nDone when: checked"))
        self.assertTrue(check_in["text"].endswith("https://teem.test" + headers["Location"]))
        run_id = headers["Location"].split("/")[-1]
        self.assertEqual(check_in["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"r:{run_id}:1:a")

    def test_voice_request_waits_for_button_and_reaches_ready(self):
        self.enable_decider()
        proposal = ("propose_run", {"repo": self.project_id.upper(), "objective": "Change value",
                                    "acceptance_criteria": "value.txt contains after"})
        self.model.responses = [("I can set that up.", proposal)]
        voice = {"file_id": "v1", "duration": 1, "file_size": len(self.api.state["voice"])}
        self.deliver(message(300, voice=voice))
        run = self.run_row()
        self.assertEqual((run["status"], run["project_id"]), ("awaiting_approval", self.project_id))
        self.assertEqual(self.texts()[:2], ["Heard: approve change value", "I can set that up."])
        self.assertEqual(self.api.state["sent"][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         f"r:{run['id']}:1:a")
        self.assertEqual(self.model.requests[0]["messages"][-1], {"role": "user", "content": "approve change value"})
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM approvals WHERE run_id=%s", (run["id"],)).fetchone()["count"], 0)
            # Reprocessing the same update (a crash before commit) cannot create a second Run.
            conn.execute("UPDATE telegram_updates SET processed_at=NULL WHERE update_id=300")
        self.model.responses = [("Again.", proposal)]
        self.drain()
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM runs WHERE project_id=%s", (self.project_id,)).fetchone()["count"], 1)

        self.deliver(callback(301, f"r:{run['id']}:1:a", sender=999), callback(302, f"r:{run['id']}:2:a"),
                     callback(303, f"r:{run['id']}:1:a"), callback(304, f"r:{run['id']}:1:d"))
        self.assertIn("That decision no longer applies (decision is stale).", self.texts())
        self.assertEqual(self.texts()[-2:], ["Approved. The run is queued.",
                                             "That decision no longer applies (decision is stale)."])
        answered = [c[1]["callback_query_id"] for c in self.api.state["calls"] if c[0] == "answerCallbackQuery"]
        self.assertEqual(answered, ["cb302", "cb303", "cb304"])
        with connect(self.dsn) as conn:
            approval = conn.execute("SELECT decision,source FROM approvals WHERE run_id=%s", (run["id"],)).fetchone()
        self.assertEqual((approval["decision"], approval["source"]), ("approve", "telegram_button"))
        self.claim_and_execute()
        self.claim_and_execute()
        self.drain()
        self.assertTrue(self.texts()[-1].startswith("Ready to merge: Test: Change value"))

    def test_grant_starts_runs_without_asking_until_revoked(self):
        self.enable_decider()
        run_tool = ("propose_run", {"repo": self.project_id, "objective": "Change value",
                                    "acceptance_criteria": "value.txt contains after"})
        self.model.responses = [("", ("propose_project", {"repo": "someone-else/repo"})),
                                ("Sure.", ("propose_project", {"repo": self.project_id}))]
        self.deliver(message(400, text="let teem work on someone else's repo"),
                     message(401, text="you can always work on my test repo"))
        self.assertEqual(self.texts()[0], "I can only work on GitHub repositories owned by teem-test.")
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM projects WHERE id LIKE 'someone-else/%%'").fetchone()["count"], 0)
            grant = conn.execute("SELECT grant_id FROM projects WHERE id=%s", (self.project_id,)).fetchone()["grant_id"]
        prompt = self.api.state["sent"][-1]
        self.assertTrue(prompt["text"].startswith(f"Allow Teem to start runs on {self.project_id}"))
        self.assertEqual(prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"p:{grant}:a")

        self.model.responses = [("Starting.", run_tool)]
        self.deliver(callback(402, f"p:{grant}:a"), message(403, text="change the value, approve it"))
        run = self.run_row()
        self.assertEqual(run["status"], "queued")
        self.assertEqual(self.texts()[-1], f"Started on {self.project_id}: Change value")
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT source FROM approvals WHERE run_id=%s", (run["id"],)).fetchone()["source"],
                             f"project_grant:{grant}")
        state = json.loads(self.model.requests[-1]["messages"][0]["content"].split("Current state:\n", 1)[1])
        self.assertIn({"repo": self.project_id, "allowed_without_asking": True}, state["projects"])

        self.model.responses = [("", ("cancel_run", {"run_id": str(run["id"])})), ("Queued for approval.", run_tool)]
        self.deliver(message(404, text=f"/revoke {self.project_id}"), message(405, text="stop that run"),
                     callback(406, f"p:{grant}:a"), message(407, text="change the value again"))
        self.assertEqual(self.texts()[-5:-1], [f"Revoked. Teem will ask before each run on {self.project_id}.",
                                               "Cancellation requested.", "That request no longer applies.",
                                               "Queued for approval."])
        self.assertTrue(self.texts()[-1].startswith("Decision required:"))
        self.assertEqual(self.run_row()["status"], "awaiting_approval")


if __name__ == "__main__":
    unittest.main()
