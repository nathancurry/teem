"""Slice-3 acceptance paths against real HTTP, PostgreSQL, FFmpeg, and Bubblewrap."""

import base64
import hashlib
import http.client
import json
import os
import socket
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import http_ece
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02

from teem.db import connect, event
from teem.push import send_due
from teem.server import App, Handler, ThreadingHTTPServer, expire_leases, stop_run
from tests import test_slice as slice1


class Slice3Acceptance(unittest.TestCase):
    setUpClass = classmethod(slice1.SliceAcceptance.setUpClass.__func__)
    tearDownClass = classmethod(slice1.SliceAcceptance.tearDownClass.__func__)
    setUp = slice1.SliceAcceptance.setUp
    tearDown = slice1.SliceAcceptance.tearDown
    stop_server = slice1.SliceAcceptance.stop_server
    browser = slice1.SliceAcceptance.browser
    claim_and_execute = slice1.SliceAcceptance.claim_and_execute
    propose = slice1.SliceAcceptance.propose

    def start_server(self):
        if not hasattr(self, "speech_config"):
            model = self.root / "model.bin"
            model.write_bytes(b"test fixture model")
            self.runner = self.root / "whisper-cli"
            self.set_runner("approve change value")
            self.speech_config = self.root / "speech.json"
            self.speech_config.write_text(json.dumps({"executable": str(self.runner), "model": str(model),
                "executable_sha256": hashlib.sha256(self.runner.read_bytes()).hexdigest(),
                "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(), "language": "en"}))
            vapid = Vapid02()
            vapid.generate_keys()
            self.vapid_key = self.root / "vapid.pem"
            vapid.save_key(str(self.vapid_key))
        args = type("Args", (), {"dsn": self.dsn, "artifacts": str(self.root / "artifacts"),
            "username": "user", "password": "password", "worker_id": "worker",
            "worker_token": "worker-secret", "origin": "https://teem.test",
            "reviewer_config": str(self.reviewer_file), "speech_config": str(self.speech_config),
            "speech_scratch": str(self.root / "speech-scratch"),
            "vapid_private_key": str(self.vapid_key), "vapid_subject": "mailto:test@example.com"})()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = App(args)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def set_runner(self, text=None, slow=False):
        program = ("#!/usr/bin/python3\nfrom pathlib import Path\nimport time\n" +
                   ("time.sleep(120)\n" if slow else f"Path('/scratch/transcript.txt').write_text({text!r})\n"))
        self.runner.write_text(program)
        self.runner.chmod(0o755)
        if hasattr(self, "speech_config"):
            config = json.loads(self.speech_config.read_text())
            config["executable_sha256"] = hashlib.sha256(self.runner.read_bytes()).hexdigest()
            self.speech_config.write_text(json.dumps(config))

    def audio(self, kind, seconds=1):
        codec, container = ("libopus", "webm") if kind == "audio/webm" else ("aac", "mp4")
        command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                   "anullsrc=r=16000:cl=mono", "-t", str(seconds), "-c:a", codec, "-b:a", "16k"]
        if container == "mp4":
            command += ["-movflags", "frag_keyframe+empty_moov"]
        return subprocess.run(command + ["-f", container, "pipe:1"], capture_output=True, check=True).stdout

    def raw_request(self, method, path, body=b"", content_type="application/json", auth=True, origin="https://teem.test"):
        parsed = urlparse(self.url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
        headers = {"Content-Type": content_type, "Origin": origin}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(b"user:password").decode()
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def subscribe(self, endpoint):
        self.push_private = ec.generate_private_key(ec.SECP256R1())
        key = self.push_private.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint)
        self.push_auth = os.urandom(16)
        data = {"endpoint": endpoint, "keys": {"p256dh": base64.urlsafe_b64encode(key).rstrip(b"=").decode(),
                "auth": base64.urlsafe_b64encode(self.push_auth).rstrip(b"=").decode()}}
        status, _, body = self.raw_request("POST", "/push/subscribe", json.dumps(data).encode())
        self.assertEqual(status, 200, body)
        return json.loads(body)["id"], data

    def test_phone_input_requires_existing_approval(self):
        clip = self.audio("audio/webm")
        self.assertEqual(self.raw_request("POST", "/transcribe", clip, "audio/webm", auth=False)[0], 401)
        self.assertEqual(self.raw_request("POST", "/transcribe", clip, "audio/webm", origin="https://other.test")[0], 403)
        status, _, body = self.raw_request("POST", "/transcribe", clip, "audio/webm")
        self.assertEqual(status, 200, body)
        self.assertIn("approve", json.loads(body)["text"])
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM requests WHERE text LIKE '%approve change value%'").fetchone()["count"], 0)
            self.assertEqual(conn.execute("""SELECT count(*) FROM approvals a JOIN runs r ON r.id=a.run_id
                WHERE r.project_id=%s""", (self.project_id,)).fetchone()["count"], 0)
        real = [os.environ.get(name) for name in ("TEEM_REAL_WHISPER_BIN", "TEEM_REAL_WHISPER_MODEL", "TEEM_REAL_SPEECH_WAV")]
        if all(real):
            executable, model, sample = map(Path, real)
            self.speech_config.write_text(json.dumps({"executable": str(executable),
                "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "model": str(model), "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(), "language": "en"}))
            self.stop_server()
            self.start_server()
            self.worker.api.url = self.url
            for kind, codec, container in (("audio/webm", "libopus", "webm"), ("audio/mp4", "aac", "mp4")):
                command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(sample), "-c:a", codec]
                if container == "mp4":
                    command += ["-movflags", "frag_keyframe+empty_moov"]
                recorded = subprocess.run(command + ["-f", container, "pipe:1"], capture_output=True, check=True).stdout
                status, _, body = self.raw_request("POST", "/transcribe", recorded, kind)
                self.assertEqual(status, 200, body)
                self.assertIn("country", json.loads(body)["text"].lower())
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after", "dedupe_key": "dictation-1"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after", "dedupe_key": "dictation-1"})[1]["Location"], path)
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 409)
        self.claim_and_execute()
        self.claim_and_execute()
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT status FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()["status"], "ready_to_merge")
            self.assertEqual(conn.execute("SELECT count(*) FROM approvals WHERE run_id=%s", (path.split("/")[-1],)).fetchone()["count"], 1)

    def test_speech_environment_isolated(self):
        self.runner.write_text("#!/usr/bin/python3\nimport os\nfrom pathlib import Path\n"
                               "Path('/scratch/transcript.txt').write_text("
                               "os.environ.get('TEEM_SPEECH_SECRET_SENTINEL', 'absent'))\n")
        self.runner.chmod(0o755)
        config = json.loads(self.speech_config.read_text())
        config["executable_sha256"] = hashlib.sha256(self.runner.read_bytes()).hexdigest()
        self.speech_config.write_text(json.dumps(config))
        self.stop_server()
        self.start_server()
        clip = self.audio("audio/webm")
        with patch.dict(os.environ, {"TEEM_SPEECH_SECRET_SENTINEL": "must-not-pass"}):
            status, _, body = self.raw_request("POST", "/transcribe", clip, "audio/webm")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["text"], "absent")

    def test_audio_failure_privacy_restart_and_fallback(self):
        subprocess.run(["node", str(Path(__file__).with_name("phone_browser.cjs"))], check=True)
        for kind in ("audio/webm", "audio/mp4"):
            status, _, body = self.raw_request("POST", "/transcribe", self.audio(kind), kind)
            self.assertEqual(status, 200, body)
        self.assertEqual(self.raw_request("POST", "/transcribe", self.audio("audio/webm", 120), "audio/webm")[0], 200)
        self.assertEqual(self.raw_request("POST", "/transcribe", b"junk", "audio/webm")[0], 400)
        self.assertEqual(self.raw_request("POST", "/transcribe", self.audio("audio/webm", 121), "audio/webm")[0], 400)
        parsed = urlparse(self.url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            conn.putrequest("POST", "/transcribe")
            conn.putheader("Authorization", "Basic " + base64.b64encode(b"user:password").decode())
            conn.putheader("Origin", "https://teem.test")
            conn.putheader("Content-Type", "audio/webm")
            conn.putheader("Content-Length", str(8 * 1024 * 1024 + 1))
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 413)
        finally:
            conn.close()
        self.assertEqual(list((self.root / "speech-scratch").iterdir()), [])
        with patch("teem.speech.RUNNER_SECONDS", 0.5):
            self.set_runner(slow=True)
            status, _, _ = self.raw_request("POST", "/transcribe", self.audio("audio/webm"), "audio/webm")
            self.assertEqual(status, 504)
        self.assertEqual(list((self.root / "speech-scratch").iterdir()), [])
        self.set_runner(slow=True)
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.raw_request(
            "POST", "/transcribe", self.audio("audio/webm"), "audio/webm")))
        thread.start()
        for _ in range(100):
            if self.server.app.speech.process:
                break
            time.sleep(0.02)
        self.assertIsNotNone(self.server.app.speech.process)
        self.assertEqual(self.raw_request("POST", "/transcribe", self.audio("audio/webm"), "audio/webm")[0], 429)
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Typed fallback", "criteria": "checked", "dedupe_key": "fallback"})
        self.assertEqual(status, 303)
        self.assertEqual(self.browser("POST", headers["Location"] + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        self.stop_server()
        thread.join(5)
        self.start_server()
        self.worker.api.url = self.url
        self.assertEqual(list((self.root / "speech-scratch").iterdir()), [])
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM requests").fetchone()["count"], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM approvals").fetchone()["count"], 1)
        self.claim_and_execute()
        self.claim_and_execute()

    def test_invalid_push_key_rejected_and_persisted_key_removed(self):
        invalid_key = base64.urlsafe_b64encode(b"\x04" + b"\x00" * 64).rstrip(b"=").decode()
        with patch("teem.push.valid_endpoint", return_value=True):
            sub_id, data = self.subscribe("https://fcm.googleapis.com/fcm/send/test")
            invalid = {**data, "keys": {**data["keys"], "p256dh": invalid_key}}
            self.assertEqual(self.raw_request("POST", "/push/subscribe", json.dumps(invalid).encode())[0], 400)
            with connect(self.dsn) as conn:
                self.assertEqual(conn.execute("SELECT p256dh FROM push_subscriptions WHERE id=%s",
                                              (sub_id,)).fetchone()["p256dh"], data["keys"]["p256dh"])
            self.assertEqual(self.browser("POST", "/requests", {"project": self.project_id,
                "objective": "Check invalid key", "criteria": "checked", "dedupe_key": "bad-push-key"})[0], 303)
            with connect(self.dsn) as conn:
                conn.execute("UPDATE push_subscriptions SET p256dh=%s WHERE id=%s", (invalid_key, sub_id))
                self.assertEqual(conn.execute("SELECT count(*) FROM notification_deliveries WHERE subscription_id=%s",
                                              (sub_id,)).fetchone()["count"], 1)
            with patch("teem.push.send_push") as send:
                self.assertTrue(send_due(self.server.app))
                send.assert_not_called()
            with connect(self.dsn) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM push_subscriptions WHERE id=%s",
                                              (sub_id,)).fetchone()["count"], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM notification_deliveries WHERE subscription_id=%s",
                                              (sub_id,)).fetchone()["count"], 0)

    def test_notification_outbox_retry_replay_expiry_and_current_state(self):
        received = []
        responses = [201, 503, "drop", 201, 201, 410]
        owner = self

        class PushEndpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                encrypted = self.rfile.read(length)
                received.append(http_ece.decrypt(encrypted, private_key=owner.push_private,
                                                 auth_secret=owner.push_auth).decode())
                response = responses.pop(0)
                if response == "drop":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(response)
                self.send_header("Retry-After", "180")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass

        endpoint = HTTPServer(("127.0.0.1", 0), PushEndpoint)
        endpoint_thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        endpoint_thread.start()
        try:
            with patch("teem.push.valid_endpoint", return_value=True):
                sub_id, data = self.subscribe(f"http://127.0.0.1:{endpoint.server_port}/push")
                self.assertEqual(self.raw_request("POST", "/push/subscribe", json.dumps(data).encode(), auth=False)[0], 401)
                self.assertEqual(self.raw_request("POST", "/push/subscribe", json.dumps(data).encode(), origin="https://other.test")[0], 403)
                self.assertEqual(self.subscribe(f"http://127.0.0.1:{endpoint.server_port}/push")[0], sub_id)
                status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
                    "objective": "First", "criteria": "checked", "dedupe_key": "push-1"})
                self.assertEqual(status, 303)
                run_id = headers["Location"].split("/")[-1]
                with connect(self.dsn) as conn:
                    count = conn.execute("SELECT count(*) FROM notification_deliveries").fetchone()["count"]
                    self.assertEqual(count, 1)
                self.assertEqual(self.browser("POST", "/requests", {"project": self.project_id,
                    "objective": "First", "criteria": "checked", "dedupe_key": "push-1"})[0], 303)
                with connect(self.dsn) as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM notification_deliveries").fetchone()["count"], count)
                    try:
                        with conn.transaction():
                            event(conn, run_id, "rolled_back", {}, notify=True)
                            raise RuntimeError
                    except RuntimeError:
                        conn.rollback()
                    self.assertEqual(conn.execute("SELECT count(*) FROM notification_deliveries").fetchone()["count"], count)
                self.stop_server()
                self.start_server()
                self.worker.api.url = self.url

                self.assertTrue(send_due(self.server.app))
                with connect(self.dsn) as conn:
                    self.assertEqual(conn.execute("SELECT state FROM notification_deliveries").fetchone()["state"], "accepted")
                self.assertEqual(received, [run_id])

                self.assertEqual(self.browser("POST", f"/runs/{run_id}/approve", {"version": "1", "decision": "approve"})[0], 303)
                assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
                    "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
                with connect(self.dsn) as conn:
                    conn.execute("UPDATE attempts SET lease_until=now()-interval '1 second' WHERE id=%s", (assignment["attempt_id"],))
                    expire_leases(conn)
                    self.assertEqual(conn.execute("SELECT status FROM runs WHERE id=%s", (run_id,)).fetchone()["status"], "uncertain")
                    self.assertEqual(conn.execute("""SELECT count(*) FROM notification_deliveries d JOIN events e
                        ON e.id=d.event_id WHERE e.run_id=%s""", (run_id,)).fetchone()["count"], 2)
                self.assertTrue(send_due(self.server.app))
                with connect(self.dsn) as conn:
                    self.assertGreater(conn.execute("""SELECT extract(epoch FROM (next_attempt_at-now())) AS seconds
                        FROM notification_deliveries WHERE state='pending' ORDER BY event_id LIMIT 1""").fetchone()["seconds"], 170)
                    conn.execute("UPDATE notification_deliveries SET next_attempt_at=now()-interval '1 second' WHERE state='pending'")
                self.assertTrue(send_due(self.server.app))
                with connect(self.dsn) as conn:
                    conn.execute("UPDATE notification_deliveries SET next_attempt_at=now()-interval '1 second' WHERE state='pending'")
                self.assertTrue(send_due(self.server.app))
                self.assertEqual(received[-3:], [run_id, run_id, run_id])
                with connect(self.dsn) as conn:
                    stop_run(conn, run_id, "test_stop")
                    conn.execute("UPDATE events SET created_at=now()-interval '25 hours' WHERE id=(SELECT max(event_id) FROM notification_deliveries)")
                self.assertFalse(send_due(self.server.app))
                with connect(self.dsn) as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM notification_deliveries WHERE state='abandoned'").fetchone()["count"], 1)
                status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
                    "objective": "Second", "criteria": "checked", "dedupe_key": "push-2"})
                self.assertEqual(status, 303)
                second = headers["Location"].split("/")[-1]
                self.assertEqual(self.browser("POST", f"/runs/{second}/approve", {"version": "1", "decision": "approve"})[0], 303)
                self.claim_and_execute()
                self.claim_and_execute()
                with connect(self.dsn) as conn:
                    self.assertEqual(conn.execute("SELECT status FROM runs WHERE id=%s", (second,)).fetchone()["status"], "ready_to_merge")
                state = json.loads(self.browser("GET", f"/runs/{second}/state")[2])
                self.assertEqual(state["runs"][0]["status"], "ready_to_merge")
                with connect(self.dsn) as conn:
                    conn.execute("UPDATE notification_deliveries SET next_attempt_at=now()-interval '1 second' WHERE state='pending'")
                send_due(self.server.app)
                send_due(self.server.app)
                with connect(self.dsn) as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM push_subscriptions WHERE id=%s", (sub_id,)).fetchone()["count"], 0)
                    self.assertEqual(conn.execute("SELECT count(*) FROM notification_deliveries WHERE subscription_id=%s", (sub_id,)).fetchone()["count"], 0)
        finally:
            endpoint.shutdown()
            endpoint.server_close()
            endpoint_thread.join()
