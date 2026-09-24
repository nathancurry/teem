"""One acceptance path against a disposable PostgreSQL database and real sandbox."""

import base64
import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import urlparse

import psycopg
from psycopg.conninfo import make_conninfo

from teem.db import connect, initialize
from teem.server import App, Handler, ThreadingHTTPServer
from teem.worker import Worker, WorkerError, restricted_run


def run(*argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class SliceAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("TEEM_TEST_DSN"):
            raise unittest.SkipTest("set TEEM_TEST_DSN to a PostgreSQL admin connection")
        cls.admin_dsn = os.environ["TEEM_TEST_DSN"]
        cls.dbname = "teem_test_" + os.urandom(6).hex()
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{cls.dbname}"')
        cls.dsn = make_conninfo(cls.admin_dsn, dbname=cls.dbname)
        initialize(cls.dsn)

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "dbname"):
            return
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{cls.dbname}" WITH (FORCE)')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_id = "test-" + os.urandom(4).hex()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run("git", "init", "-q", str(self.repo))
        (self.repo / "value.txt").write_text("before\n")
        (self.repo / "coder.py").write_text(
            "from pathlib import Path\n"
            "import time\n"
            "time.sleep(1.5)\n"
            "try:\n"
            "    Path('.git/config').write_text('tampered')\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('Git metadata was writable')\n"
            "Path('value.txt').write_text('after\\n')\n"
        )
        (self.repo / "check.py").write_text("from pathlib import Path\nassert Path('value.txt').read_text() == 'after\\n'\n")
        run("git", "add", ".", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base", cwd=self.repo)
        self.base = run("git", "rev-parse", "HEAD", cwd=self.repo)
        self.checks = [{"name": "content", "argv": ["/usr/bin/python3", "/workspace/check.py"]}]
        from teem.common import canonical, digest
        with connect(self.dsn) as conn:
            conn.execute("INSERT INTO projects(id,name,base_commit,checks,check_hash) VALUES (%s,'Test',%s,%s::jsonb,%s)",
                         (self.project_id, self.base, canonical(self.checks), digest(self.checks)))
        projects = {self.project_id: {"repo": str(self.repo), "coder": ["/usr/bin/python3", "/workspace/coder.py"], "checks": self.checks}}
        config = self.root / "projects.json"
        config.write_text(json.dumps(projects))
        args = type("Args", (), {"url": "https://example.invalid", "token": "worker-secret", "worker_id": "worker",
                                    "state_dir": str(self.root / "worker"), "projects": str(config)})()
        self.worker_args = args
        self.worker = Worker(args)
        self.start_server()
        self.worker.api.url = self.url

    def tearDown(self):
        self.stop_server()
        self.worker.journal.db.close()
        self.temp.cleanup()

    def start_server(self):
        args = type("Args", (), {"dsn": self.dsn, "artifacts": str(self.root / "artifacts"),
                                    "username": "user", "password": "password", "worker_id": "worker",
                                    "worker_token": "worker-secret", "origin": "https://teem.test"})()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = App(args)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def restart_worker(self):
        self.worker.journal.db.close()
        self.worker = Worker(self.worker_args)
        self.worker.api.url = self.url

    def browser(self, method, path, body=None):
        from urllib.parse import urlencode
        parsed = urlparse(self.url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        headers = {"Authorization": "Basic " + base64.b64encode(b"user:password").decode(),
                   "Origin": "https://teem.test"}
        if body is not None:
            body = urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def test_authorization_restart_reconnect_and_artifact(self):
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id, "objective": "Change value",
            "criteria": "value.txt contains after", "dedupe_key": "request-1"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        run_id = path.split("/")[-1]
        self.assertEqual(self.browser("POST", "/requests", {"project": self.project_id, "objective": "Change value",
            "criteria": "value.txt contains after", "dedupe_key": "request-1"})[1]["Location"], path)
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"])
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM approvals").fetchone()["count"], 0)
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"]
        redelivered = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"]
        self.assertEqual(redelivered, assignment)
        self.assertTrue(self.worker.journal.accept(assignment))
        self.assertFalse(self.worker.journal.accept(assignment))
        original = self.worker.api.call
        blocked = {"once": True}
        restarted = threading.Event()

        def restart_during_coding():
            self.stop_server()
            self.start_server()
            self.worker.api.url = self.url
            restarted.set()

        def disconnect_on_upload(method, endpoint, data=None, sha=None):
            if endpoint.startswith("/worker/upload/") and blocked["once"]:
                blocked["once"] = False
                raise WorkerError("disconnected after execution")
            return original(method, endpoint, data, sha)

        self.worker.api.call = disconnect_on_upload
        timer = threading.Timer(0.3, restart_during_coding)
        timer.start()
        self.worker.execute(assignment)
        timer.join(timeout=5)
        self.assertTrue(restarted.is_set())
        self.worker.api.call = original
        with connect(self.dsn) as conn:
            attempt = conn.execute("SELECT status,usage FROM attempts WHERE id=%s", (assignment["attempt_id"],)).fetchone()
            self.assertEqual(attempt["status"], "running", attempt["usage"])
            self.assertEqual(conn.execute("SELECT count(*) FROM candidates").fetchone()["count"], 0)
            conn.execute("UPDATE attempts SET lease_until=now()-interval '1 second' WHERE id=%s", (assignment["attempt_id"],))
        self.assertIn(b"uncertain", self.browser("GET", path)[2])
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"])
        self.assertEqual(self.browser("POST", path + "/retry", {})[0], 409)
        self.restart_worker()
        self.worker.reconcile()
        status, _, body = self.browser("GET", path)
        self.assertEqual(status, 200)
        self.assertIn(b"awaiting_review", body)
        with connect(self.dsn) as conn:
            candidate = conn.execute("SELECT * FROM candidates WHERE run_id=%s", (run_id,)).fetchone()
            self.assertEqual(candidate["evidence"]["checks"][0]["exit_code"], 0)
            self.assertEqual(candidate["evidence"]["head_commit"], candidate["head_commit"])
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()["count"], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE run_id=%s", (run_id,)).fetchone()["count"] > 5, True)
        import shutil
        shutil.rmtree(self.root / "worker" / ("attempt-" + assignment["attempt_id"]))
        status, _, bundle = self.browser("GET", f"/candidates/{candidate['id']}/bundle")
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(bundle).hexdigest(), candidate["artifact_sha256"])
        bundle_file = self.root / "retrieved.bundle"
        bundle_file.write_bytes(bundle)
        retrieved = self.root / "retrieved"
        run("git", "clone", "-q", str(bundle_file), str(retrieved))
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=retrieved), candidate["head_commit"])
        self.assertEqual((retrieved / "value.txt").read_text(), "after\n")

    def test_failed_checks_preserve_candidate_and_cancel_stops_dispatch(self):
        from teem.common import canonical, digest
        self.checks = [{"name": "failing check", "argv": ["/usr/bin/false"]}]
        self.worker.projects[self.project_id]["checks"] = self.checks
        with connect(self.dsn) as conn:
            conn.execute("UPDATE projects SET checks=%s::jsonb,check_hash=%s WHERE id=%s",
                         (canonical(self.checks), digest(self.checks), self.project_id))
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(assignment))
        self.worker.execute(assignment)
        self.assertIn(b"checks_failed", self.browser("GET", path)[2])
        with connect(self.dsn) as conn:
            candidate = conn.execute("SELECT evidence FROM candidates WHERE attempt_id=%s", (assignment["attempt_id"],)).fetchone()
            self.assertNotEqual(candidate["evidence"]["checks"][0]["exit_code"], 0)
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Another change", "criteria": "new result"})
        self.assertEqual(status, 303)
        cancel_path = headers["Location"]
        self.assertEqual(self.browser("POST", cancel_path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        self.assertEqual(self.browser("POST", cancel_path + "/cancel", {})[0], 303)
        self.assertIn(b"cancelled", self.browser("GET", cancel_path)[2])
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"])

    def test_expired_accepted_attempt_requires_reconciliation_before_retry(self):
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(assignment))
        with connect(self.dsn) as conn:
            conn.execute("UPDATE attempts SET lease_until=now()-interval '1 second' WHERE id=%s", (assignment["attempt_id"],))
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"])
        self.assertEqual(self.browser("POST", path + "/retry", {})[0], 409)
        self.restart_worker()
        self.worker.reconcile()
        self.assertFalse((self.root / "worker" / ("attempt-" + assignment["attempt_id"])).exists())
        with connect(self.dsn) as conn:
            attempt = conn.execute("SELECT status FROM attempts WHERE id=%s", (assignment["attempt_id"],)).fetchone()
            self.assertEqual(attempt["status"], "reconciled_uncertain")
        self.assertEqual(self.browser("POST", path + "/retry", {})[0], 303)
        replacement = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"]
        self.assertNotEqual(replacement["attempt_id"], assignment["attempt_id"])
        self.assertEqual(replacement["generation"], assignment["generation"] + 1)
        with self.assertRaises(WorkerError):
            self.worker.api.call("POST", "/worker/report/" + assignment["attempt_id"],
                                 {"generation": assignment["generation"], "outcome": "failed"})
        self.assertEqual(self.browser("POST", path + "/cancel", {})[0], 303)

    def test_worker_process_crash_reconciles_without_duplicate_coder(self):
        coder = self.repo / "coder.py"
        coder.write_text(coder.read_text().replace(
            "time.sleep(1.5)",
            "with Path('starts.txt').open('a') as marker:\n    marker.write('start\\n')\ntime.sleep(30)",
        ))
        run("git", "add", "coder.py", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "long coder", cwd=self.repo)
        base = run("git", "rev-parse", "HEAD", cwd=self.repo)
        with connect(self.dsn) as conn:
            conn.execute("UPDATE projects SET base_commit=%s WHERE id=%s", (base, self.project_id))
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        run_id = path.split("/")[-1]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)

        script = (
            "import sys\n"
            "from types import SimpleNamespace\n"
            "from teem.worker import Worker\n"
            "args = SimpleNamespace(url='https://example.invalid', token='worker-secret', worker_id='worker', "
            "projects=sys.argv[1], state_dir=sys.argv[2])\n"
            "worker = Worker(args)\n"
            "worker.api.url = sys.argv[3]\n"
            "worker.reconcile()\n"
            "assignment = worker.api.call('POST', '/worker/claim', "
            "{'worker_id': 'worker', 'capabilities': ['code', 'check', 'bundle']})['assignment']\n"
            "if assignment and worker.journal.accept(assignment):\n"
            "    worker.execute(assignment)\n"
        )
        command = [sys.executable, "-c", script, str(self.root / "projects.json"),
                   str(self.root / "worker"), self.url]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if first.poll() is not None:
                    _, stderr = first.communicate()
                    self.fail("worker exited before coder started: " + stderr.decode(errors="replace"))
                with sqlite3.connect(self.root / "worker" / "journal.sqlite3") as journal:
                    row = journal.execute("SELECT id,pid,state FROM attempts LIMIT 1").fetchone()
                if row and row[2] == "executing" and \
                   (self.root / "worker" / ("attempt-" + row[0]) / "starts.txt").exists():
                    break
                time.sleep(0.05)
            else:
                self.fail("coder did not start")
            attempt_id, process_group, _ = row
            first.kill()
            first.communicate(timeout=5)

            def old_process_running():
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
                    except OSError:
                        continue
                    if int(fields[2]) == process_group and fields[0] != "Z":
                        return True
                return False

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and old_process_running():
                time.sleep(0.05)
            self.assertFalse(old_process_running(), "old coder process group survived worker death")

            with connect(self.dsn) as conn:
                conn.execute("UPDATE attempts SET lease_until=now()-interval '1 second' WHERE id=%s", (attempt_id,))
            self.assertIn(b"uncertain", self.browser("GET", path)[2])
            restarted = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(restarted.returncode, 0, restarted.stderr.decode(errors="replace"))
            with connect(self.dsn) as conn:
                attempt = conn.execute("SELECT status FROM attempts WHERE id=%s", (attempt_id,)).fetchone()
                self.assertEqual(attempt["status"], "reconciled_uncertain")
                self.assertEqual(conn.execute("SELECT count(*) FROM attempts WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE run_id=%s)", (run_id,)).fetchone()["count"], 1)
            marker = self.root / "worker" / ("attempt-" + attempt_id) / "starts.txt"
            self.assertEqual(marker.read_text(), "start\n")
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate(timeout=5)

    def test_journal_failure_after_launch_stops_subprocess(self):
        class RejectJournal:
            pid = None

            def update(self, attempt_id, state, pid=None):
                self.pid = pid
                raise OSError("journal unavailable")

        journal = RejectJournal()
        contract = self.root / "contract.json"
        contract.write_text("{}")
        with self.assertRaises(OSError):
            restricted_run(["/usr/bin/python3", "-c", "import time; time.sleep(30)"],
                           self.repo, contract, 30, lambda: None, journal, "test-attempt")
        self.assertIsNotNone(journal.pid)
        with self.assertRaises(ProcessLookupError):
            os.killpg(journal.pid, 0)

    def test_transport_loss_during_coding_is_uncertain(self):
        coder = self.repo / "coder.py"
        coder.write_text(coder.read_text().replace("time.sleep(1.5)", "time.sleep(30)"))
        run("git", "add", "coder.py", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "long coder", cwd=self.repo)
        base = run("git", "rev-parse", "HEAD", cwd=self.repo)
        with connect(self.dsn) as conn:
            conn.execute("UPDATE projects SET base_commit=%s WHERE id=%s", (base, self.project_id))
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(assignment))
        original = self.worker.api.call
        renewals = 0

        def disconnect_on_renew(method, endpoint, data=None, sha=None):
            nonlocal renewals
            if endpoint.startswith("/worker/renew/"):
                renewals += 1
                if renewals == 4:
                    raise URLError("worker connection lost")
            return original(method, endpoint, data, sha)

        self.worker.api.call = disconnect_on_renew
        self.worker.execute(assignment)
        self.assertEqual(renewals, 4)
        with connect(self.dsn) as conn:
            attempt = conn.execute("SELECT status FROM attempts WHERE id=%s", (assignment["attempt_id"],)).fetchone()
            self.assertEqual(attempt["status"], "reconciled_uncertain")
            run_row = conn.execute("SELECT status FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()
            self.assertEqual(run_row["status"], "uncertain")

    def test_cancellation_waits_for_worker_confirmation(self):
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(assignment))
        observed = {}

        def cancel_during_coding():
            deadline = time.monotonic() + 5
            with sqlite3.connect(self.root / "worker" / "journal.sqlite3") as journal:
                while time.monotonic() < deadline:
                    state = journal.execute("SELECT state FROM attempts WHERE id=?", (assignment["attempt_id"],)).fetchone()[0]
                    if state == "executing":
                        break
                    time.sleep(0.02)
                else:
                    observed["error"] = "coder never started"
                    return
            observed["response"] = self.browser("POST", path + "/cancel", {})[0]
            with connect(self.dsn) as conn:
                observed["status"] = conn.execute("SELECT status FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()["status"]

        canceller = threading.Thread(target=cancel_during_coding)
        canceller.start()
        self.worker.execute(assignment)
        canceller.join(timeout=5)
        self.assertFalse(canceller.is_alive())
        self.assertNotIn("error", observed)
        self.assertEqual(observed["response"], 303)
        self.assertEqual(observed["status"], "cancelling")
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts WHERE id=%s", (assignment["attempt_id"],)).fetchone()["status"], "cancelled")
            self.assertEqual(conn.execute("SELECT count(*) FROM candidates WHERE run_id=%s", (path.split("/")[-1],)).fetchone()["count"], 0)
        self.assertIn(b"cancelled", self.browser("GET", path)[2])

    def test_cancellation_survives_concurrent_claim(self):
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)

        claim_paused = threading.Event()
        resume_claim = threading.Event()
        cancel_entered = threading.Event()
        cancel_has_run = threading.Event()
        resume_cancel = threading.Event()

        class PausedConnection:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, query, params=None):
                if query.startswith("UPDATE runs SET status='coding'"):
                    claim_paused.set()
                    if not resume_claim.wait(10):
                        raise TimeoutError("claim was not resumed")
                if query == "SELECT * FROM runs WHERE id=%s FOR UPDATE":
                    cancel_entered.set()
                result = self.connection.execute(query, params)
                if query == "SELECT * FROM runs WHERE id=%s FOR UPDATE":
                    cancel_has_run.set()
                    if not resume_cancel.wait(10):
                        raise TimeoutError("cancellation was not resumed")
                return result

        observed = {}

        def claim():
            try:
                observed["assignment"] = self.worker.api.call("POST", "/worker/claim",
                    {"worker_id": "worker", "capabilities": ["code", "check", "bundle"]})["assignment"]
            except Exception as exc:
                observed["claim_error"] = exc

        def cancel():
            try:
                observed["cancel_status"] = self.browser("POST", path + "/cancel", {})[0]
            except Exception as exc:
                observed["cancel_error"] = exc

        claim_thread = threading.Thread(target=claim)
        cancel_thread = threading.Thread(target=cancel)
        with patch("teem.server.connect", side_effect=lambda dsn: PausedConnection(connect(dsn))):
            try:
                claim_thread.start()
                self.assertTrue(claim_paused.wait(5))
                cancel_thread.start()
                self.assertTrue(cancel_entered.wait(5))
                cancel_has_run.wait(1)
                resume_claim.set()
                self.assertTrue(cancel_has_run.wait(5))
                resume_cancel.set()
            finally:
                resume_claim.set()
                resume_cancel.set()
                claim_thread.join(10)
                if cancel_thread.ident is not None:
                    cancel_thread.join(10)

        self.assertFalse(claim_thread.is_alive())
        self.assertFalse(cancel_thread.is_alive())
        self.assertNotIn("claim_error", observed)
        self.assertNotIn("cancel_error", observed)
        self.assertIsNotNone(observed.get("assignment"))
        self.assertEqual(observed.get("cancel_status"), 303)
        with connect(self.dsn) as conn:
            run = conn.execute("SELECT status FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()
            task = conn.execute("SELECT status,cancel_requested FROM tasks WHERE run_id=%s", (path.split("/")[-1],)).fetchone()
        self.assertEqual(run["status"], "cancelling")
        self.assertEqual(task, {"status": "cancelling", "cancel_requested": True})

    def test_missing_journaled_bundle_reconciles_uncertain(self):
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(assignment))
        original = self.worker.api.call

        def disconnect_on_upload(method, endpoint, data=None, sha=None):
            if endpoint.startswith("/worker/upload/"):
                raise URLError("worker connection lost")
            return original(method, endpoint, data, sha)

        self.worker.api.call = disconnect_on_upload
        self.worker.execute(assignment)
        (self.root / "worker" / (assignment["attempt_id"] + ".bundle")).unlink()
        self.restart_worker()
        self.worker.reconcile()
        with connect(self.dsn) as conn:
            attempt = conn.execute("SELECT status FROM attempts WHERE id=%s", (assignment["attempt_id"],)).fetchone()
            self.assertEqual(attempt["status"], "reconciled_uncertain")
            self.assertEqual(conn.execute("SELECT count(*) FROM candidates WHERE run_id=%s", (path.split("/")[-1],)).fetchone()["count"], 0)
        self.assertIn(b"uncertain", self.browser("GET", path)[2])


if __name__ == "__main__":
    unittest.main()
