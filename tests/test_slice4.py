"""Slice-4 acceptance paths against real HTTP, PostgreSQL, FFmpeg, Bubblewrap, and a fake Bot API."""

import hashlib
import http.client
import json
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer

import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlencode, urlparse

from teem import github, telegram
from teem.db import connect
from teem.worker import container_command, restricted_run
from teem.workflow import create_run
from tests.test_slice import run

FAKE_CLAUDE = """#!/usr/bin/python3
import json, sys
from pathlib import Path
prompt = sys.argv[sys.argv.index('-p') + 1]
assert '--dangerously-skip-permissions' in sys.argv
if '--json-schema' in sys.argv:
    schema = json.loads(sys.argv[sys.argv.index('--json-schema') + 1])
    numbers = schema['properties']['findings']['items']['properties']['criterion_number']['enum']
    if Path('/workspace/value.txt').read_text() == 'after\\n':
        judgment = {'verdict': 'changes_required', 'summary': 'Value is not reviewed', 'uncertainties': [],
                    'findings': [{'criterion_number': numbers[1], 'description': 'value.txt must say reviewed',
                                  'evidence': [{'kind': 'source', 'path': 'value.txt', 'start_line': 1,
                                                'end_line': 1, 'check_name': None}],
                                  'reproduction': {'path': 'test_probe.py', 'content': 'assert False',
                                                   'output': 'AssertionError: plain after'}}]}
    else:
        judgment = {'verdict': 'pass', 'summary': 'Reviewed value present', 'findings': [], 'uncertainties': []}
    print(json.dumps({'type': 'result', 'is_error': False, 'result': json.dumps(judgment),
                      'structured_output': judgment}))
    sys.exit()
if 'Ask first' in prompt:
    model = sys.argv[sys.argv.index('--model') + 1] if '--model' in sys.argv else 'default'
    print(json.dumps({'type': 'result', 'is_error': False, 'result': f'Which file should change? ({model})'}))
    sys.exit()
if 'Reproduction test test_probe.py' in prompt and 'AssertionError: plain after' in prompt:
    value = 'after reviewed'
elif \"Check 'content' failed\" in prompt:
    value = 'after'
else:
    value = 'wrong'
Path('value.txt').write_text(value + '\\n')
print(json.dumps({'type': 'result', 'is_error': False, 'result': 'Set value.txt to ' + value}))
"""

FAKE_CODEX = """#!/usr/bin/python3
import json, sys
from pathlib import Path
args = sys.argv
assert args[1] == 'exec' and args[args.index('-C') + 1] == '/workspace'
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
numbers = schema['properties']['findings']['items']['properties']['criterion_number']['enum']
assert all(type(n) is int for n in numbers), 'criteria are chosen by number, never quoted text'
value = Path('/workspace/value.txt').read_text()
if value == 'after\\n':
    judgment = {'verdict': 'changes_required', 'summary': 'Value is not reviewed', 'uncertainties': [],
                'findings': [{'criterion_number': numbers[1], 'description': 'value.txt must say reviewed',
                              'evidence': [{'kind': 'source', 'path': 'value.txt', 'start_line': 1, 'end_line': 1,
                                            'check_name': None}],
                              'reproduction': {'path': 'test_probe.py', 'content': 'assert False',
                                               'output': 'AssertionError: plain after'}}]}
else:
    judgment = {'verdict': 'pass', 'summary': 'Reviewed value present', 'findings': [], 'uncertainties': []}
Path(args[args.index('-o') + 1]).write_text(json.dumps(judgment))
"""
from teem.server import App, Handler, ThreadingHTTPServer
from tests import test_slice as slice1

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


class FakeGitHub(BaseHTTPRequestHandler):
    def do_GET(self):
        head = self.path.split("head=", 1)[1].split("&", 1)[0].replace("%3A", ":").replace("%2F", "/")
        self.reply(200, [{**pr, "state": pr.get("state", "open")} for pr in self.server.pulls if pr["head"] == head])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.headers["Authorization"] == "Bearer test-token"
        if self.server.fail_posts:
            self.server.fail_posts -= 1
            return self.reply(502, {})
        owner = self.path.split("/")[2]
        pr = {**body, "head": owner + ":" + body["head"],
              "html_url": f"https://github.test/{owner}/pull/{len(self.server.pulls) + 1}"}
        self.server.pulls.append(pr)
        self.reply(201, pr)

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
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
    reviewer_verdict = slice1.SliceAcceptance.reviewer_verdict
    propose = slice1.SliceAcceptance.propose

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
        self.github_api = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.github_api.pulls, self.github_api.fail_posts = [], 0
        threading.Thread(target=self.github_api.serve_forever, daemon=True).start()
        self.addCleanup(self.github_api.server_close)
        self.addCleanup(self.github_api.shutdown)
        slice1.SliceAcceptance.setUp(self)
        with connect(self.dsn) as conn:
            conn.execute("DELETE FROM telegram_outbox")
            conn.execute("DELETE FROM telegram_updates")
            # Earlier tests share this database; keep their ready Runs out of this test's publisher.
            conn.execute("UPDATE runs SET next_publish_at='infinity' WHERE status='ready_to_merge'")

    def start_server(self):
        if not hasattr(self, "speech_config"):
            model = self.root / "model.bin"
            model.write_bytes(b"test fixture model")
            self.runner = self.root / "whisper-cli"
            self.speech_config = self.root / "speech.json"
            self.set_runner("approve change value")
        args = type("Args", (), {"dsn": self.dsn, "artifacts": str(self.root / "artifacts"),
            "username": "user", "password": "password", "worker_id": "worker",
            "worker_token": "worker-secret", "origin": "https://teem.test",
            "reviewer_config": str(self.reviewer_file), "speech_config": str(self.speech_config),
            "speech_scratch": str(self.root / "speech-scratch"), "github_config": str(self.github_file)})()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = App(args)
        self.server.app.git_base = str(self.git_base)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        app = self.server.app
        app.telegram_api = f"http://127.0.0.1:{self.api.server_port}"
        app.telegram_token, app.telegram_user_id = "123:secret", USER
        app.github_api, app.github_token = f"http://127.0.0.1:{self.github_api.server_port}", "test-token"
        if self.use_decider:
            app.decider_api = f"http://127.0.0.1:{self.model.server_port}"
            app.decider = {"api_key": "key", "model": "test-model", "timeout": 10}

    def set_runner(self, text=None, slow=False):
        """A stand-in whisper-cli; FFmpeg validation and the Bubblewrap sandbox around it are real."""
        program = ("#!/usr/bin/python3\nfrom pathlib import Path\nimport time\n" +
                   ("time.sleep(120)\n" if slow else f"Path('/scratch/transcript.txt').write_text({text!r})\n"))
        self.runner.write_text(program)
        self.runner.chmod(0o755)
        self.speech_config.write_text(json.dumps({
            "executable": str(self.runner), "model": str(self.root / "model.bin"),
            "executable_sha256": hashlib.sha256(self.runner.read_bytes()).hexdigest(),
            "model_sha256": hashlib.sha256((self.root / "model.bin").read_bytes()).hexdigest(), "language": "en"}))
        if hasattr(self, "server"):
            self.stop_server()
            self.start_server()
            self.worker.api.url = self.url

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
                                    "acceptance_criteria": "value.txt contains after", "model": "opus"})
        self.model.responses = [("I can set that up.", proposal)]
        voice = {"file_id": "v1", "duration": 1, "file_size": len(self.api.state["voice"])}
        self.deliver(message(300, voice=voice))
        run = self.run_row()
        self.assertEqual((run["status"], run["project_id"]), ("awaiting_approval", self.project_id))
        self.assertEqual(self.texts()[:2], ["Heard: approve change value", "I can set that up."])
        self.assertEqual(self.api.state["sent"][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         f"r:{run['id']}:1:a")
        self.assertIn("model: opus", self.api.state["sent"][2]["text"])
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
        self.assertTrue(github.publish_due(self.server.app))
        self.drain()
        self.assertEqual(self.texts()[-1], "Pull request open: Test: Change value\nhttps://github.test/teem-test/pull/1")

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
        self.assertEqual(self.texts()[-5], f"Revoked. Teem will ask before each run on {self.project_id}.")
        self.assertTrue(self.texts()[-4].startswith("Cancelled: Test: Change value"))
        self.assertEqual(self.texts()[-3:-1], ["That request no longer applies.", "Queued for approval."])
        self.assertTrue(self.texts()[-1].startswith("Decision required:"))
        self.assertEqual(self.run_row()["status"], "awaiting_approval")

    def test_agent_container_reaches_only_the_proxy(self):
        probe = (
            "import os, socket\n"
            "print(os.environ.get('TEEM_HOST_SECRET', 'no host env'))\n"
            "host, port = os.environ['HTTPS_PROXY'].removeprefix('http://').split(':')\n"
            "for target in ('github.com', '192.168.1.1'):\n"
            "    s = socket.create_connection((host, int(port)), timeout=5)\n"
            "    s.sendall(f'CONNECT {target}:443 HTTP/1.1\\r\\nHost: {target}:443\\r\\n\\r\\n'.encode())\n"
            "    print(s.recv(100).split(b'\\r\\n')[0].decode())\n"
            "for address in (('1.1.1.1', 443), ('192.168.1.1', 80)):\n"
            "    try:\n"
            "        socket.create_connection(address, timeout=2)\n"
            "        print('direct open')\n"
            "    except OSError:\n"
            "        print('direct blocked')\n"
            "try:\n"
            "    socket.getaddrinfo('example.com', 443)\n"
            "    print('dns open')\n"
            "except OSError:\n"
            "    print('dns blocked')\n")
        attempt = "probe-" + os.urandom(4).hex()
        command = container_command(self.worker.policy, "teem-" + attempt, attempt,
                                    ["/usr/bin/python3", "-c", probe], [], "/tmp", 30)
        with patch.dict(os.environ, {"TEEM_HOST_SECRET": "leaked"}):
            code, output = restricted_run(command, attempt, 30, lambda: None)
        self.assertEqual(code, 0, output)
        self.assertEqual(output.split("\n")[:6], ["no host env", "HTTP/1.1 403 Filtered", "HTTP/1.1 403 Filtered",
                                                  "direct blocked", "direct blocked", "dns blocked"])

    def use_agent_wrappers(self, reviewer="reviewer_codex.py"):
        fakebin = self.repo / "fakebin"
        fakebin.mkdir()
        for name, program in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            (fakebin / name).write_text(program)
            (fakebin / name).chmod(0o755)
        (self.repo / "check.py").write_text("from pathlib import Path\nassert Path('value.txt').read_text().startswith('after')\n")
        run("git", "add", ".", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fake agents", cwd=self.repo)
        path = {"PATH": "/workspace/fakebin:/usr/local/bin:/usr/bin:/bin"}
        self.worker.policy["coder"] = {"argv": ["teem-implement"], "env": path}
        self.worker.policy["reviewer"].update(
            executable=str(Path(__file__).resolve().parent.parent / "teem" / reviewer), env=path)

    def test_agent_wrappers_revise_on_failed_checks_and_review(self):
        self.use_agent_wrappers()
        # Quoted examples in criteria are common and must survive the reviewer's strict output schema.
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": 'value.txt starts with "after"', "revisions": "2"})
        run_id = headers["Location"].split("/")[-1]
        self.assertEqual(self.browser("POST", headers["Location"] + "/approve",
                                      {"version": "1", "decision": "approve"})[0], 303)
        for _ in range(5):
            self.claim_and_execute()
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT status FROM runs WHERE id=%s", (run_id,)).fetchone()["status"],
                             "ready_to_merge")
            reasons = [row["payload"].get("reason") for row in conn.execute(
                "SELECT payload FROM events WHERE run_id=%s AND kind='revision_queued' ORDER BY id", (run_id,))]
            self.assertEqual(reasons, ["checks_failed", None])
            reviews = conn.execute("""SELECT v.result FROM reviews v JOIN attempts a ON a.id=v.attempt_id
                                      JOIN tasks t ON t.id=a.task_id WHERE t.run_id=%s ORDER BY v.created_at""",
                                   (run_id,)).fetchall()
            self.assertEqual([r["result"]["verdict"] for r in reviews], ["changes_required", "pass"])
            self.assertEqual(reviews[0]["result"]["findings"][0]["reproduction"]["path"], "test_probe.py")
            summaries = [row["usage"]["summary"] for row in conn.execute(
                """SELECT a.usage FROM attempts a JOIN tasks t ON t.id=a.task_id
                   WHERE t.run_id=%s AND t.kind='code_and_check' ORDER BY t.revision_number""", (run_id,))]
            self.assertEqual(summaries, ["Set value.txt to wrong", "Set value.txt to after",
                                         "Set value.txt to after reviewed"])

    def test_claude_reviewer_requests_changes_then_passes(self):
        self.use_agent_wrappers(reviewer="reviewer_claude.py")
        run_id = self.propose(revisions=2).split("/")[-1]
        for _ in range(5):
            self.claim_and_execute()
        with connect(self.dsn) as conn:
            self.assertEqual(self.run_status(run_id)["status"], "ready_to_merge")
            verdicts = [r["result"]["verdict"] for r in conn.execute(
                """SELECT v.result FROM reviews v JOIN attempts a ON a.id=v.attempt_id
                   JOIN tasks t ON t.id=a.task_id WHERE t.run_id=%s ORDER BY v.created_at""", (run_id,))]
        self.assertEqual(verdicts, ["changes_required", "pass"])

    def test_implementer_without_changes_asks_the_user(self):
        self.use_agent_wrappers()
        base, checks = github.fetch_base(self.server.app, self.project_id)
        with connect(self.dsn) as conn:
            run_id, _ = create_run(conn, self.project_id, base, checks, self.reviewer_config,
                                   "Ask first", "value.txt changes", "ask-first", model="opus")
        self.assertEqual(self.browser("POST", f"/runs/{run_id}/approve", {"version": "1", "decision": "approve"})[0], 303)
        self.claim_and_execute()
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"])
        self.drain()
        self.assertIn("Stopped: Test: Ask first\nStop reason: needs_input: Which file should change? (opus)", self.texts()[-1])

    def test_voice_failures_reply_and_leave_no_audio(self):
        state = self.api.state
        voice = {"file_id": "v1", "duration": 1, "file_size": 10}
        good = state["voice"]
        state["voice"] = b"not audio"
        self.deliver(message(500, voice=voice))
        self.assertIn("couldn't transcribe that voice note (invalid recording)", self.texts()[-1])
        state["voice"] = good
        self.set_runner(slow=True)
        with patch("teem.speech.RUNNER_SECONDS", 0.5):
            self.deliver(message(501, voice=voice))
        self.assertIn("couldn't transcribe that voice note (transcription timed out)", self.texts()[-1])
        self.deliver(message(502, voice={**voice, "duration": 121}))
        self.assertIn("limited to 120 seconds", self.texts()[-1])
        self.assertEqual(list((self.root / "speech-scratch").iterdir()), [])

    def run_status(self, run_id):
        with connect(self.dsn) as conn:
            return conn.execute("SELECT status,stop_reason,pr_url FROM runs WHERE id=%s", (run_id,)).fetchone()

    def test_publication_pushes_the_reviewed_candidate_once(self):
        run_id = self.propose().split("/")[-1]
        self.claim_and_execute()
        self.claim_and_execute()
        self.github_api.fail_posts = 1
        self.assertTrue(github.publish_due(self.server.app))
        self.assertEqual(self.run_status(run_id)["status"], "ready_to_merge")
        self.assertFalse(github.publish_due(self.server.app), "a transient failure waits for its backoff")
        with connect(self.dsn) as conn:
            conn.execute("UPDATE runs SET next_publish_at=now() WHERE id=%s", (run_id,))
        self.assertTrue(github.publish_due(self.server.app))
        self.assertEqual(self.run_status(run_id), {"status": "pr_open", "stop_reason": None,
                                                   "pr_url": "https://github.test/teem-test/pull/1"})
        with connect(self.dsn) as conn:
            head = conn.execute("""SELECT x.head_commit FROM runs r JOIN candidates x ON x.id=r.current_candidate_id
                                   WHERE r.id=%s""", (run_id,)).fetchone()["head_commit"]
        self.assertEqual(run("git", "rev-parse", f"teem/{run_id}", cwd=self.repo), head)
        pull = self.github_api.pulls[0]
        self.assertEqual((pull["title"], pull["head"]), ("Teem: Change value", f"teem-test:teem/{run_id}"))
        self.assertIn("**Independent review**: pass. Criteria met", pull["body"])
        # A crash after opening but before recording the PR repeats publication without a second PR.
        with connect(self.dsn) as conn:
            conn.execute("UPDATE runs SET status='ready_to_merge',pr_url=NULL,next_publish_at=NULL WHERE id=%s", (run_id,))
        self.assertTrue(github.publish_due(self.server.app))
        self.assertEqual(len(self.github_api.pulls), 1)
        self.assertEqual(self.run_status(run_id)["pr_url"], "https://github.test/teem-test/pull/1")

    def test_revoked_grant_stops_publication(self):
        with connect(self.dsn) as conn:
            conn.execute("UPDATE projects SET status='granted' WHERE id=%s", (self.project_id,))
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        run_id = headers["Location"].split("/")[-1]
        self.claim_and_execute()
        self.claim_and_execute()
        with connect(self.dsn) as conn:
            conn.execute("UPDATE projects SET status='revoked' WHERE id=%s", (self.project_id,))
        self.assertTrue(github.publish_due(self.server.app))
        self.assertEqual(self.run_status(run_id)["stop_reason"], "grant_revoked")
        self.assertEqual(self.github_api.pulls, [])
        self.assertEqual(run("git", "branch", "--list", "teem/*", cwd=self.repo), "")

    def request(self, method, path, body=None, headers=None):
        parsed = urlparse(self.url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        conn.request(method, path, urlencode(body) if body is not None else None,
                     {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
        response = conn.getresponse()
        result = response.status, response.headers, response.read()
        conn.close()
        return result

    def test_sign_in_form_sets_a_session_for_pages_and_decisions(self):
        run_path = self.propose(revisions=0)
        status, headers, _ = self.request("GET", run_path)
        self.assertEqual((status, headers["Location"]), (303, "/login?next=" + quote(run_path, safe="")))
        self.assertEqual(self.request("GET", "/state")[0], 401)
        form = {"username": "user", "password": "password", "next": run_path}
        origin = {"Origin": "https://teem.test"}
        self.assertEqual(self.request("POST", "/login", form)[0], 403)
        status, headers, _ = self.request("POST", "/login", {**form, "password": "wrong"}, origin)
        self.assertEqual((status, headers["Set-Cookie"]), (401, None))
        status, headers, _ = self.request("POST", "/login", {**form, "next": "//evil.test/"}, origin)
        self.assertEqual(headers["Location"], "/")
        status, headers, _ = self.request("POST", "/login", form, origin)
        self.assertEqual((status, headers["Location"]), (303, run_path))
        cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", run_path, headers=cookie)[0], 200)
        self.assertEqual(self.request("POST", run_path + "/cancel", {}, cookie)[0], 403)
        self.assertEqual(self.request("POST", run_path + "/cancel", {}, {**cookie, **origin})[0], 303)
        expires, signature = cookie["Cookie"].split("=", 1)[1].split(".")
        forged = {"Cookie": f"teem_session={int(expires) + 1}.{signature}"}
        self.assertEqual(self.request("GET", run_path, headers=forged)[0], 303)

    def test_replacing_a_running_run_waits_for_the_stop_and_confirms(self):
        self.enable_decider()
        with connect(self.dsn) as conn:
            conn.execute("UPDATE projects SET status='granted' WHERE id=%s", (self.project_id,))
        first = {"repo": self.project_id, "objective": "Change value", "acceptance_criteria": "value.txt contains after"}
        self.model.responses = [("Starting.", ("propose_run", first))]
        self.deliver(message(600, text="change the value"))
        old = self.run_row()
        attempt = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.model.responses = [("Rerunning with Opus.", ("propose_run", {**first, "model": "opus",
                                                                          "replaces_run_id": str(old["id"])}))]
        self.deliver(message(601, text="cancel that and rerun it on opus"))
        self.assertEqual(self.texts()[-2:], ["Rerunning with Opus.",
                                             "Stopping the current run. The new one starts as soon as it has stopped."])
        self.assertEqual(self.run_row()["id"], old["id"], "the replacement waits until the old run has stopped")
        self.worker.api.call("POST", "/worker/report/" + attempt["attempt_id"],
                             {"generation": attempt["generation"], "outcome": "cancelled"})
        self.drain()
        new = self.run_row()
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(self.run_status(old["id"])["status"], "cancelled")
        self.assertEqual(new["status"], "queued")
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT body FROM contracts WHERE run_id=%s", (new["id"],)).fetchone()
                             ["body"]["implementer_model"], "opus")
        self.assertTrue(self.texts()[-2].startswith("Cancelled: Test: Change value"))
        self.assertTrue(self.texts()[-1].startswith("Queued: Test: Change value"))
        # Leave nothing claimable for later tests sharing this database.
        self.assertEqual(self.browser("POST", f"/runs/{new['id']}/cancel", {})[0], 303)

    def test_show_run_resends_pending_decision_buttons(self):
        self.enable_decider()
        proposal = {"repo": self.project_id, "objective": "Change value", "acceptance_criteria": "value.txt contains after"}
        self.model.responses = [("", ("propose_run", proposal))]
        self.deliver(message(700, text="change the value"))
        run = self.run_row()
        self.model.responses = [("Here it is again.", ("show_run", {"run_id": str(run["id"])}))]
        self.deliver(message(701, text="there's no approve button"))
        resent = self.api.state["sent"][-1]
        self.assertTrue(resent["text"].startswith("Decision required: Test: Change value"))
        self.assertEqual(resent["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"r:{run['id']}:1:a")

    def test_reviewer_failure_reports_its_error_and_retries_only_the_review(self):
        self.reviewer.write_text("#!/usr/bin/python3\nimport sys\n"
                                 "sys.stderr.write('codex failed: 401 Unauthorized\\n')\nsys.exit(1)\n")
        self.reviewer.chmod(0o755)
        run_id = self.propose().split("/")[-1]
        coding = self.claim_and_execute()
        self.claim_and_execute()
        self.claim_and_execute()
        self.drain()
        failure = self.api.state["sent"][-1]
        self.assertIn("Error: codex failed: 401 Unauthorized", failure["text"])
        self.assertEqual(failure["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"v:{run_id}")
        self.reviewer_verdict("pass")
        self.deliver(callback(800, f"v:{run_id}"), callback(801, f"v:{run_id}"))
        self.assertEqual(self.texts()[-2:], ["Retrying the review of the same Candidate.",
                                             "That decision no longer applies (only a Run stopped by a failed "
                                             "review can retry its review)."])
        review = self.claim_and_execute()
        self.assertEqual(review["kind"], "review")
        self.assertEqual(self.run_status(run_id)["status"], "ready_to_merge")
        with connect(self.dsn) as conn:
            codings = conn.execute("""SELECT count(*) FROM attempts a JOIN tasks t ON t.id=a.task_id
                                      WHERE t.run_id=%s AND t.kind='code_and_check'""", (run_id,)).fetchone()
        self.assertEqual(codings["count"], 1, "the implementation was not redone")
        self.assertIsNotNone(coding)

    def test_revision_limit_stop_shows_findings_and_can_publish_anyway(self):
        self.reviewer_verdict("changes_required")
        run_id = self.propose(revisions=0).split("/")[-1]
        self.claim_and_execute()
        self.claim_and_execute()
        self.drain()
        stop = self.api.state["sent"][-1]
        self.assertIn("Stop reason: revision_limit\nReview: Review completed\n- Outcome missing", stop["text"])
        self.assertEqual(stop["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"u:{run_id}")
        self.deliver(callback(900, f"u:{run_id}"))
        self.assertEqual(self.texts()[-1], "Publishing it as a pull request marked as not passing review.")
        self.assertTrue(github.publish_due(self.server.app))
        pull = self.github_api.pulls[-1]
        self.assertEqual(pull["title"], "Teem (review not passed): Change value")
        self.assertIn("did not pass independent review", pull["body"])
        self.assertIn("- Outcome missing", pull["body"])

    def test_quiet_worker_alerts_once_per_waiting_run(self):
        run_id = self.propose().split("/")[-1]
        app = self.server.app

        def alerts():
            app.alert_idle_worker()
            self.drain()
            return [text for text in self.texts() if text.startswith("No worker has picked up this run")]

        try:
            self.assertEqual(alerts(), [], "a recently seen worker is not an outage")
            with connect(self.dsn) as conn:
                conn.execute("UPDATE runs SET updated_at=now()-interval '11 minutes' WHERE id=%s", (run_id,))
            app.worker_seen -= 600
            self.assertEqual(len(alerts()), 1)
            self.assertEqual(len(alerts()), 1, "one alert per waiting Run")
        finally:
            # Leave nothing claimable for later tests sharing this database.
            self.browser("POST", f"/runs/{run_id}/cancel", {})
    def test_merged_pull_requests_and_models_reach_the_stats_page(self):
        self.worker.policy["coder"]["env"] = {"TEEM_CLAUDE_MODEL": "sonnet"}
        run_id = self.propose().split("/")[-1]
        self.claim_and_execute()
        self.claim_and_execute()
        self.assertTrue(github.publish_due(self.server.app))
        github.check_pull_requests(self.server.app)
        self.assertEqual(self.run_status(run_id)["status"], "pr_open")
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT pr_state FROM runs WHERE id=%s", (run_id,)).fetchone()["pr_state"], "open")
            # Checks are rate-limited; make this one due again after the user merges on GitHub.
            conn.execute("UPDATE runs SET pr_checked_at=now()-interval '11 minutes' WHERE id=%s", (run_id,))
        self.github_api.pulls[-1].update(state="closed", merged_at="2026-09-26T12:00:00Z")
        github.check_pull_requests(self.server.app)
        with connect(self.dsn) as conn:
            row = conn.execute("SELECT pr_state,pr_closed_at FROM runs WHERE id=%s", (run_id,)).fetchone()
        self.assertEqual(row["pr_state"], "merged")
        self.assertIsNotNone(row["pr_closed_at"])
        status, _, body = self.browser("GET", "/stats")
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn("<td>sonnet</td>", page)
        self.assertIn("pull requests merged:", page)


if __name__ == "__main__":
    unittest.main()
