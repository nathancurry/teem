"""One acceptance path against a disposable PostgreSQL database and real sandbox."""

import base64
import hashlib
import http.client
import json
import os
import shutil
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
from teem.worker import Worker, WorkerError, container_command, restricted_run

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "worker"
# Tag test images by their inputs so an edited Containerfile or wrapper is rebuilt, not reused.
IMAGE_TAG = hashlib.sha256(b"".join((DEPLOY / name).read_bytes() for name in
                                    ("agent.Containerfile", "teem-implement", "proxy.Containerfile",
                                     "tinyproxy.conf"))).hexdigest()[:12]
AGENT_IMAGE = os.environ.get("TEEM_TEST_AGENT_IMAGE", "localhost/teem-agent:test-" + IMAGE_TAG)
PROXY_IMAGE = "localhost/teem-proxy:test-" + IMAGE_TAG


def podman(*argv, check=True):
    return subprocess.run(["podman", *argv], check=check, capture_output=True, text=True).stdout.strip()


def start_agent_network(allowlist):
    """An internal network whose only exit is a fresh allowlisting proxy, as deployed on a worker."""
    for image, containerfile in ((AGENT_IMAGE, "agent.Containerfile"), (PROXY_IMAGE, "proxy.Containerfile")):
        if subprocess.run(["podman", "image", "exists", image]).returncode:
            podman("build", "-q", "-t", image, "-f", str(DEPLOY / containerfile), str(DEPLOY))
    suffix = os.urandom(3)
    name = "teem-test-" + suffix.hex()
    subnet = f"10.{200 + suffix[0] % 50}.{suffix[1]}"
    podman("network", "create", "--internal", "--disable-dns", "--subnet", subnet + ".0/24", name)
    podman("run", "-d", "--rm", "--name", name + "-proxy", "--network", "podman",
           "--network", f"{name}:ip={subnet}.2", "-v", f"{allowlist}:/etc/tinyproxy/allow:ro,z", PROXY_IMAGE)
    return name, f"http://{subnet}.2:8888"


def stop_agent_network(name):
    podman("rm", "--force", "--time", "0", name + "-proxy", check=False)
    podman("network", "rm", "--force", name, check=False)


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
        cls.allowlist = Path(tempfile.mkdtemp()) / "allow"
        cls.allowlist.write_text("^example\\.com$\n")
        cls.agent_network, cls.agent_proxy = start_agent_network(cls.allowlist)

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "dbname"):
            return
        stop_agent_network(cls.agent_network)
        shutil.rmtree(cls.allowlist.parent, ignore_errors=True)
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{cls.dbname}" WITH (FORCE)')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_id = "teem-test/repo-" + os.urandom(4).hex()
        # A local directory stands in for github.com; the repository is reached as <base>/<owner>/<name>.git.
        self.git_base = self.root / "github"
        self.repo = self.git_base / (self.project_id + ".git")
        self.repo.mkdir(parents=True)
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
        self.checks = [{"name": "content", "argv": ["/usr/bin/python3", "/workspace/check.py"]}]
        (self.repo / ".teem").mkdir()
        (self.repo / ".teem" / "checks.json").write_text(json.dumps(self.checks))
        run("git", "add", ".", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base", cwd=self.repo)
        self.base = run("git", "rev-parse", "HEAD", cwd=self.repo)
        from teem.common import canonical, digest
        self.reviewer_config = {"identity": "test-reviewer-v1", "instructions": "Review the exact candidate against the criteria.",
                                "destination": "local", "timeout": 30}
        self.reviewer_file = self.root / "reviewer.json"
        self.reviewer_file.write_text(json.dumps(self.reviewer_config))
        self.reviewer = self.root / "reviewer"
        self.reviewer.write_text(
            "#!/usr/bin/python3\nimport json\n"
            "with open('/context.json') as f: context=json.load(f)\n"
            "pack=context['pack']\n"
            "print(json.dumps({'candidate_id':pack['candidate_id'],"
            "'contract_version':pack['contract_version'],'context_sha256':context['sha256'],"
            "'verdict':'pass','summary':'Criteria met','findings':[],'uncertainties':[]}))\n")
        self.reviewer.chmod(0o755)
        reviewer_policy = {"identity": self.reviewer_config["identity"],
                           "instructions_sha256": hashlib.sha256(self.reviewer_config["instructions"].encode()).hexdigest(),
                           "destination": "local", "timeout": 30, "executable": str(self.reviewer)}
        with connect(self.dsn) as conn:
            conn.execute("INSERT INTO projects(id,name,status) VALUES (%s,'Test','proposed')", (self.project_id,))
        policy = {"owners": ["teem-test"], "coder": {"argv": ["/usr/bin/python3", "/workspace/coder.py"]},
                  "reviewer": reviewer_policy, "git_base": str(self.git_base), "image": AGENT_IMAGE,
                  "network": self.agent_network, "proxy": self.agent_proxy}
        config = self.root / "projects.json"
        config.write_text(json.dumps(policy))
        self.github_file = self.root / "github.json"
        self.github_file.write_text(json.dumps({"owners": ["teem-test"]}))
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
                                    "worker_token": "worker-secret", "origin": "https://teem.test",
                                    "reviewer_config": str(self.reviewer_file),
                                    "github_config": str(self.github_file)})()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = App(args)
        self.server.app.git_base = str(self.git_base)
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

    def propose(self, revisions=2):
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains a reviewed result", "revisions": str(revisions)})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        return path

    def claim_and_execute(self):
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertIsNotNone(assignment)
        self.assertTrue(self.worker.journal.accept(assignment))
        self.worker.execute(assignment)
        return assignment

    def reviewer_verdict(self, verdict):
        self.reviewer.write_text(
            "#!/usr/bin/python3\nimport json\nfrom pathlib import Path\n"
            "context=json.loads(Path('/context.json').read_text())\npack=context['pack']\n"
            "finding={'criterion':pack['acceptance_criteria'],'description':'Outcome missing',"
            "'evidence':[{'kind':'source','path':'value.txt','start_line':1,'end_line':1}]}\n"
            f"verdict={verdict!r}\n"
            "print(json.dumps({'candidate_id':pack['candidate_id'],"
            "'contract_version':pack['contract_version'],'context_sha256':context['sha256'],"
            "'verdict':verdict,'summary':'Review completed',"
            "'findings':[finding] if verdict=='changes_required' else [],"
            "'uncertainties':['Cannot establish criterion'] if verdict=='blocked' else []}))\n")
        self.reviewer.chmod(0o755)

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
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"])
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM approvals WHERE run_id=%s",
                                          (run_id,)).fetchone()["count"], 0)
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        redelivered = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
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
            self.assertEqual(conn.execute("SELECT count(*) FROM candidates WHERE run_id=%s",
                                          (run_id,)).fetchone()["count"], 0)
            conn.execute("UPDATE attempts SET lease_until=now()-interval '1 second' WHERE id=%s", (assignment["attempt_id"],))
        self.assertIn(b"uncertain", self.browser("GET", path)[2])
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"])
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
            self.assertEqual(conn.execute("""SELECT count(*) FROM attempts a JOIN tasks t ON t.id=a.task_id
                                             WHERE t.run_id=%s""", (run_id,)).fetchone()["count"], 1)
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
        self.checks = [{"name": "failing check", "argv": ["/usr/bin/false"]}]
        (self.repo / ".teem" / "checks.json").write_text(json.dumps(self.checks))
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qam", "failing check", cwd=self.repo)
        # With no revision budget, failing checks stop the Run instead of queueing a revision.
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after", "revisions": "0"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
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
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"])

    def test_expired_accepted_attempt_requires_reconciliation_before_retry(self):
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(assignment))
        with connect(self.dsn) as conn:
            conn.execute("UPDATE attempts SET lease_until=now()-interval '1 second' WHERE id=%s", (assignment["attempt_id"],))
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"])
        self.assertEqual(self.browser("POST", path + "/retry", {})[0], 409)
        self.restart_worker()
        self.worker.reconcile()
        self.assertFalse((self.root / "worker" / ("attempt-" + assignment["attempt_id"])).exists())
        with connect(self.dsn) as conn:
            attempt = conn.execute("SELECT status FROM attempts WHERE id=%s", (assignment["attempt_id"],)).fetchone()
            self.assertEqual(attempt["status"], "reconciled_uncertain")
        self.assertEqual(self.browser("POST", path + "/retry", {})[0], 303)
        replacement = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
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
            "{'worker_id': 'worker', 'capabilities': ['code', 'check', 'bundle', 'review']})['assignment']\n"
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
            attempt_id = row[0]
            first.kill()
            first.communicate(timeout=5)

            def containers():
                return podman("ps", "-a", "-q", "--filter", "label=teem.attempt=" + attempt_id)

            # A container outlives its crashed worker (bounded by its Podman timeout).
            self.assertTrue(containers())

            with connect(self.dsn) as conn:
                conn.execute("UPDATE attempts SET lease_until=now()-interval '1 second' WHERE id=%s", (attempt_id,))
            self.assertIn(b"uncertain", self.browser("GET", path)[2])
            restarted = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(restarted.returncode, 0, restarted.stderr.decode(errors="replace"))
            self.assertEqual(containers(), "", "restarted worker left the orphaned coder running")
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
        attempt = "journal-" + os.urandom(4).hex()
        command = container_command(self.worker.policy, "teem-" + attempt, attempt,
                                    ["/usr/bin/python3", "-c", "import time; time.sleep(30)"], [], "/tmp", 30)
        with self.assertRaises(OSError):
            restricted_run(command, attempt, 30, lambda: None, journal)
        self.assertIsNotNone(journal.pid)
        with self.assertRaises(ProcessLookupError):
            os.killpg(journal.pid, 0)
        self.assertEqual(podman("ps", "-a", "-q", "--filter", "label=teem.attempt=" + attempt), "")

    def test_transport_loss_during_coding_is_uncertain(self):
        coder = self.repo / "coder.py"
        coder.write_text(coder.read_text().replace("time.sleep(1.5)", "time.sleep(30)"))
        run("git", "add", "coder.py", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "long coder", cwd=self.repo)
        base = run("git", "rev-parse", "HEAD", cwd=self.repo)
        status, headers, _ = self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Change value", "criteria": "value.txt contains after"})
        self.assertEqual(status, 303)
        path = headers["Location"]
        self.assertEqual(self.browser("POST", path + "/approve", {"version": "1", "decision": "approve"})[0], 303)
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
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
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
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
                if query.startswith("UPDATE runs SET status=%s") and params[0] == "coding":
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
                    {"worker_id": "worker", "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
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
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
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

    def test_review_direct_pass_and_conflicting_replay_blocks_readiness(self):
        path = self.propose(revisions=0)
        coding = self.claim_and_execute()
        self.assertEqual(coding["kind"], "code_and_check")
        self.stop_server()
        self.start_server()
        self.worker.api.url = self.url
        review = self.claim_and_execute()
        self.assertEqual(review["kind"], "review")
        self.assertIn(b"Ready to merge", self.browser("GET", path)[2])
        with connect(self.dsn) as conn:
            run_row = conn.execute("SELECT * FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()
            candidate = conn.execute("SELECT * FROM candidates WHERE id=%s", (run_row["current_candidate_id"],)).fetchone()
            evidence = conn.execute("SELECT * FROM reviews WHERE attempt_id=%s", (review["attempt_id"],)).fetchone()
            self.assertEqual(evidence["disposition"], "accepted")
            self.assertEqual(evidence["result"]["candidate_id"], str(candidate["id"]))
            first_count = conn.execute("SELECT count(*) FROM events WHERE run_id=%s", (run_row["id"],)).fetchone()["count"]
        report = json.loads(self.worker.journal.db.execute("SELECT result FROM attempts WHERE id=?",
                                                      (review["attempt_id"],)).fetchone()[0])
        self.assertTrue(self.worker.api.call("POST", "/worker/report/" + review["attempt_id"],
                                             report)["replay"])
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE run_id=%s", (run_row["id"],)).fetchone()["count"], first_count)
        report["raw_output"] += " "
        with self.assertRaises(WorkerError):
            self.worker.api.call("POST", "/worker/report/" + review["attempt_id"], report)
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT status,stop_reason FROM runs WHERE id=%s", (run_row["id"],)).fetchone(),
                             {"status": "blocked", "stop_reason": "integrity_failure"})

    def test_candidate_report_conflict_before_review_pass_blocks_readiness(self):
        path = self.propose(revisions=0)
        coding = self.claim_and_execute()
        review = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        report = json.loads(self.worker.journal.db.execute("SELECT result FROM attempts WHERE id=?",
                                                      (coding["attempt_id"],)).fetchone()[0])
        report["usage"]["conflict"] = True
        with self.assertRaises(WorkerError):
            self.worker.api.call("POST", "/worker/report/" + coding["attempt_id"],
                                 {key: value for key, value in report.items() if key != "bundle"})
        self.assertTrue(self.worker.journal.accept(review))
        self.worker.execute(review)
        with connect(self.dsn) as conn:
            run_row = conn.execute("SELECT status,stop_reason FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()
            evidence = conn.execute("SELECT disposition,result FROM reviews WHERE attempt_id=%s",
                                    (review["attempt_id"],)).fetchone()
            self.assertEqual(run_row, {"status": "blocked", "stop_reason": "integrity_failure"})
            self.assertEqual((evidence["disposition"], evidence["result"]["verdict"]), ("accepted", "pass"))

    def test_cancellation_reconnect_preserves_unsent_candidate_and_review(self):
        path = self.propose()
        coding = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(coding))
        with patch.object(self.worker, "send_result", side_effect=WorkerError("ack lost")):
            self.worker.execute(coding)
        self.assertEqual(self.browser("POST", path + "/cancel", {})[0], 303)
        self.restart_worker()
        self.worker.reconcile()
        with connect(self.dsn) as conn:
            run_row = conn.execute("SELECT status,current_candidate_id FROM runs WHERE id=%s",
                                   (path.split("/")[-1],)).fetchone()
            candidate = conn.execute("SELECT id FROM candidates WHERE attempt_id=%s", (coding["attempt_id"],)).fetchone()
            self.assertEqual(run_row, {"status": "cancelled", "current_candidate_id": None})
            self.assertIsNotNone(candidate)
        self.assertEqual(self.worker.journal.db.execute("SELECT state FROM attempts WHERE id=?",
                         (coding["attempt_id"],)).fetchone()[0], "done")

        path = self.propose()
        self.claim_and_execute()
        review = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(review))
        with patch.object(self.worker, "send_result", side_effect=WorkerError("ack lost")):
            self.worker.execute(review)
        self.assertEqual(self.browser("POST", path + "/cancel", {})[0], 303)
        self.restart_worker()
        self.worker.reconcile()
        with connect(self.dsn) as conn:
            state = conn.execute("SELECT status FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()["status"]
            evidence = conn.execute("SELECT disposition,result FROM reviews WHERE attempt_id=%s",
                                    (review["attempt_id"],)).fetchone()
            self.assertEqual(state, "cancelled")
            self.assertEqual((evidence["disposition"], evidence["result"]["verdict"]), ("stale", "pass"))

    def test_current_bundle_retrieval_revokes_readiness_on_corruption_or_loss(self):
        for missing in (False, True):
            path = self.propose(revisions=0)
            self.claim_and_execute()
            self.claim_and_execute()
            with connect(self.dsn) as conn:
                run_row = conn.execute("SELECT id,current_candidate_id FROM runs WHERE id=%s",
                                       (path.split("/")[-1],)).fetchone()
                candidate = conn.execute("SELECT artifact_sha256 FROM candidates WHERE id=%s",
                                         (run_row["current_candidate_id"],)).fetchone()
            bundle = self.root / "artifacts" / (candidate["artifact_sha256"] + ".bundle")
            if missing:
                bundle.unlink()
                self.assertEqual(self.browser("GET", f"/candidates/{run_row['current_candidate_id']}/bundle")[0], 500)
            else:
                bundle.write_bytes(b"corrupt")
                with self.assertRaises(WorkerError):
                    self.worker.api.call("GET", "/worker/bundle/" + str(run_row["current_candidate_id"]))
            with connect(self.dsn) as conn:
                state = conn.execute("SELECT status,stop_reason FROM runs WHERE id=%s", (run_row["id"],)).fetchone()
                stopped = conn.execute("SELECT count(*) FROM events WHERE run_id=%s AND kind='run_stopped'",
                                       (run_row["id"],)).fetchone()["count"]
                self.assertEqual(state, {"status": "blocked", "stop_reason": "integrity_failure"})
                self.assertEqual(stopped, 1)

    def test_deadline_reconnect_preserves_late_candidate_and_review(self):
        from teem.common import digest

        path = self.propose()
        coding = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(coding))
        with patch.object(self.worker, "send_result", side_effect=WorkerError("connection lost")):
            self.worker.execute(coding)
        with connect(self.dsn) as conn:
            conn.execute("UPDATE runs SET deadline=now()-interval '1 second' WHERE id=%s", (path.split("/")[-1],))
        self.restart_worker()
        original_call = self.worker.api.call

        def reject_report(method, endpoint, data=None, sha=None):
            if endpoint.startswith("/worker/report/"):
                raise WorkerError("evidence persistence failed")
            return original_call(method, endpoint, data, sha)

        with patch.object(self.worker.api, "call", side_effect=reject_report):
            self.worker.reconcile()
        self.assertEqual(self.worker.journal.db.execute("SELECT state FROM attempts WHERE id=?",
                         (coding["attempt_id"],)).fetchone()[0], "ready_to_report")
        candidate_report = json.loads(self.worker.journal.db.execute("SELECT result FROM attempts WHERE id=?",
                                           (coding["attempt_id"],)).fetchone()[0])
        self.worker.reconcile()
        with connect(self.dsn) as conn:
            run_row = conn.execute("SELECT status,stop_reason,current_candidate_id FROM runs WHERE id=%s",
                                   (path.split("/")[-1],)).fetchone()
            candidate = conn.execute("SELECT * FROM candidates WHERE attempt_id=%s", (coding["attempt_id"],)).fetchone()
            attempt = conn.execute("SELECT status,result_sha256 FROM attempts WHERE id=%s", (coding["attempt_id"],)).fetchone()
            self.assertEqual(run_row, {"status": "blocked", "stop_reason": "deadline", "current_candidate_id": None})
            self.assertIsNotNone(candidate)
            self.assertEqual(attempt, {"status": "candidate", "result_sha256": digest({k: v for k, v in candidate_report.items()
                                                                                         if k != "bundle"})})
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks WHERE run_id=%s", (path.split("/")[-1],)).fetchone()["count"], 1)

        self.reviewer_verdict("changes_required")
        path = self.propose()
        self.claim_and_execute()
        review = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(review))
        with patch.object(self.worker, "send_result", side_effect=WorkerError("connection lost")):
            self.worker.execute(review)
        with connect(self.dsn) as conn:
            current = conn.execute("SELECT current_candidate_id FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()["current_candidate_id"]
            conn.execute("UPDATE runs SET deadline=now()-interval '1 second' WHERE id=%s", (path.split("/")[-1],))
        self.restart_worker()
        self.worker.reconcile()
        review_report = json.loads(self.worker.journal.db.execute("SELECT result FROM attempts WHERE id=?",
                                         (review["attempt_id"],)).fetchone()[0])
        with connect(self.dsn) as conn:
            run_row = conn.execute("SELECT status,stop_reason,current_candidate_id FROM runs WHERE id=%s",
                                   (path.split("/")[-1],)).fetchone()
            evidence = conn.execute("SELECT disposition,result FROM reviews WHERE attempt_id=%s", (review["attempt_id"],)).fetchone()
            attempt = conn.execute("SELECT status,result_sha256 FROM attempts WHERE id=%s", (review["attempt_id"],)).fetchone()
            self.assertEqual(run_row, {"status": "blocked", "stop_reason": "deadline", "current_candidate_id": current})
            self.assertEqual((evidence["disposition"], evidence["result"]["verdict"]), ("stale", "changes_required"))
            self.assertEqual(attempt, {"status": "review", "result_sha256": digest(review_report)})
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks WHERE run_id=%s", (path.split("/")[-1],)).fetchone()["count"], 2)
        self.assertEqual(self.worker.journal.db.execute("SELECT state FROM attempts WHERE id=?",
                         (review["attempt_id"],)).fetchone()[0], "done")

    def assert_superseded_journal_evidence(self, kind):
        from teem.common import digest

        path = self.propose()
        if kind == "review":
            self.claim_and_execute()
        assignment = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertEqual(assignment["kind"], kind)
        self.assertTrue(self.worker.journal.accept(assignment))
        with patch.object(self.worker, "send_result", side_effect=WorkerError("connection lost")):
            self.worker.execute(assignment)
        report = json.loads(self.worker.journal.db.execute("SELECT result FROM attempts WHERE id=?",
                                                     (assignment["attempt_id"],)).fetchone()[0])
        self.worker.api.call("POST", "/worker/reconcile/" + assignment["attempt_id"],
                             {"generation": assignment["generation"], "journal_state": "executing", "outcome": None})
        self.assertEqual(self.browser("POST", path + "/retry", {})[0], 303)
        replacement = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertEqual(replacement["generation"], 2)
        original_call = self.worker.api.call

        def reject_report(method, endpoint, data=None, sha=None):
            if endpoint.startswith("/worker/report/"):
                raise WorkerError("persistence unavailable")
            return original_call(method, endpoint, data, sha)

        with patch.object(self.worker.api, "call", side_effect=reject_report):
            self.worker.reconcile()
        self.assertEqual(self.worker.journal.db.execute("SELECT state FROM attempts WHERE id=?",
                         (assignment["attempt_id"],)).fetchone()[0], "ready_to_report")
        self.worker.reconcile()
        self.assertEqual(self.worker.journal.db.execute("SELECT state FROM attempts WHERE id=?",
                         (assignment["attempt_id"],)).fetchone()[0], "done")
        with connect(self.dsn) as conn:
            run_row = conn.execute("SELECT status,current_candidate_id FROM runs WHERE id=%s",
                                   (path.split("/")[-1],)).fetchone()
            attempt = conn.execute("SELECT result_sha256 FROM attempts WHERE id=%s",
                                   (assignment["attempt_id"],)).fetchone()
            self.assertEqual(attempt["result_sha256"], digest({k: v for k, v in report.items() if k != "bundle"}))
            self.assertIsNotNone(conn.execute("""SELECT 1 FROM events WHERE run_id=%s AND kind='stale_report'
                                                 AND payload->>'attempt_id'=%s""",
                                              (path.split("/")[-1], assignment["attempt_id"])).fetchone())
            self.assertEqual(run_row["status"], "coding" if kind == "code_and_check" else "reviewing")
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks WHERE run_id=%s",
                                          (path.split("/")[-1],)).fetchone()["count"], 1 if kind == "code_and_check" else 2)
            if kind == "code_and_check":
                self.assertIsNone(run_row["current_candidate_id"])
                self.assertIsNotNone(conn.execute("SELECT id FROM candidates WHERE attempt_id=%s",
                                                  (assignment["attempt_id"],)).fetchone())
            else:
                evidence = conn.execute("SELECT disposition,result FROM reviews WHERE attempt_id=%s",
                                        (assignment["attempt_id"],)).fetchone()
                self.assertEqual((evidence["disposition"], evidence["result"]["verdict"]), ("stale", "pass"))
        self.assertEqual(self.browser("POST", path + "/cancel", {})[0], 303)
        self.worker.api.call("POST", "/worker/report/" + replacement["attempt_id"],
                             {"generation": replacement["generation"], "outcome": "cancelled"})

    def test_superseded_candidate_journal_is_preserved(self):
        self.assert_superseded_journal_evidence("code_and_check")

    def test_superseded_review_journal_is_preserved(self):
        self.assert_superseded_journal_evidence("review")

    def test_changes_required_revision_uses_parent_bundle_and_fresh_review(self):
        (self.repo / "coder.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "contract=json.loads(Path('/contract.json').read_text())\n"
            "if contract.get('parent_candidate_id'):\n"
            "    assert contract['accepted_review']['verdict']=='changes_required'\n"
            "    assert contract['parent_check_evidence']['checks'][0]['exit_code']==0\n"
            "Path('value.txt').write_text('after revised\\n' if contract.get('parent_candidate_id') else 'after\\n')\n")
        (self.repo / "check.py").write_text(
            "from pathlib import Path\nassert Path('value.txt').read_text().startswith('after')\n")
        run("git", "add", ".", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "review fixture", cwd=self.repo)
        base = run("git", "rev-parse", "HEAD", cwd=self.repo)
        self.reviewer.write_text(
            "#!/usr/bin/python3\nimport json\n"
            "from pathlib import Path\n"
            "context=json.loads(Path('/context.json').read_text())\n"
            "pack=context['pack']\n"
            "assert 'accepted_review' not in context and 'source_review' not in context\n"
            "assert Path('/workspace/.git').is_dir()\n"
            "try: Path('/context.json').write_text('changed')\n"
            "except OSError: pass\n"
            "else: raise AssertionError('review input writable')\n"
            "value=Path('/workspace/value.txt').read_text().splitlines()[0]\n"
            "needs_change=value=='after'\n"
            "finding={'criterion':pack['acceptance_criteria'],'description':'Use reviewed value',"
            "'evidence':[{'kind':'source','path':'value.txt','start_line':1,'end_line':1}]}\n"
            "print(json.dumps({'candidate_id':pack['candidate_id'],"
            "'contract_version':pack['contract_version'],'context_sha256':context['sha256'],"
            "'verdict':'changes_required' if needs_change else 'pass',"
            "'summary':'Needs revision' if needs_change else 'Criteria met',"
            "'findings':[finding] if needs_change else [],'uncertainties':[]}))\n")
        self.reviewer.chmod(0o755)
        path = self.propose(revisions=1)
        first_code = self.claim_and_execute()
        first_review = self.claim_and_execute()
        self.assertIn(b"queued", self.browser("GET", path)[2])
        self.stop_server()
        self.start_server()
        self.worker.api.url = self.url
        revision = self.claim_and_execute()
        self.assertEqual(revision["revision_number"], 1)
        self.assertEqual(revision["input_candidate_id"], first_review["input_candidate_id"])
        self.assertEqual(revision["source_review_attempt_id"], first_review["attempt_id"])
        second_review = self.claim_and_execute()
        self.assertNotEqual(second_review["review_context"]["sha256"], first_review["review_context"]["sha256"])
        self.assertIn(b"Ready to merge", self.browser("GET", path)[2])
        with connect(self.dsn) as conn:
            candidates = conn.execute("SELECT * FROM candidates WHERE run_id=%s ORDER BY created_at", (path.split("/")[-1],)).fetchall()
            reviews = conn.execute("""SELECT v.* FROM reviews v JOIN attempts a ON a.id=v.attempt_id
                                      JOIN tasks t ON t.id=a.task_id WHERE t.run_id=%s ORDER BY v.created_at""",
                                   (path.split("/")[-1],)).fetchall()
            self.assertEqual([r["result"]["verdict"] for r in reviews], ["changes_required", "pass"])
            self.assertEqual(len(candidates), 2)
            self.assertNotEqual(candidates[0]["artifact_sha256"], candidates[1]["artifact_sha256"])
            current = conn.execute("SELECT current_candidate_id FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()
            self.assertEqual(current["current_candidate_id"], candidates[1]["id"])
        for assignment, expected in ((first_code, "ready_to_merge"), (revision, "blocked")):
            report = json.loads(self.worker.journal.db.execute("SELECT result FROM attempts WHERE id=?",
                                                         (assignment["attempt_id"],)).fetchone()[0])
            report["usage"]["conflict"] = True
            with self.assertRaises(WorkerError):
                self.worker.api.call("POST", "/worker/report/" + assignment["attempt_id"],
                                     {key: value for key, value in report.items() if key != "bundle"})
            with connect(self.dsn) as conn:
                self.assertEqual(conn.execute("SELECT status FROM runs WHERE id=%s",
                                              (path.split("/")[-1],)).fetchone()["status"], expected)
                self.assertEqual(conn.execute("""SELECT count(*) FROM events WHERE run_id=%s AND kind='conflicting_report'""",
                                              (path.split("/")[-1],)).fetchone()["count"],
                                 1 if expected == "ready_to_merge" else 2)

    def test_invalid_review_retries_once_then_fails(self):
        self.reviewer.write_text("#!/usr/bin/python3\nprint('not JSON')\n")
        self.reviewer.chmod(0o755)
        path = self.propose()
        self.claim_and_execute()
        first = self.claim_and_execute()
        second = self.claim_and_execute()
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(second["generation"], first["generation"] + 1)
        with connect(self.dsn) as conn:
            evidence = conn.execute("""SELECT disposition,validation_error FROM reviews
                                       WHERE attempt_id IN (%s,%s) ORDER BY created_at""",
                                    (first["attempt_id"], second["attempt_id"])).fetchall()
            self.assertEqual([x["disposition"] for x in evidence], ["invalid", "invalid"])
        self.assertIn(b"failed", self.browser("GET", path)[2])
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"])

    def test_wrong_review_identity_is_invalid_and_cannot_complete(self):
        path = self.propose()
        self.claim_and_execute()
        review = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        pack = review["review_context"]["pack"]
        report = {"outcome": "review", "generation": review["generation"],
                  "raw_output": json.dumps({"candidate_id": "00000000-0000-0000-0000-000000000000",
                                            "contract_version": pack["contract_version"],
                                            "context_sha256": review["review_context"]["sha256"],
                                            "verdict": "pass", "summary": "Wrong candidate",
                                            "findings": [], "uncertainties": []}),
                  "provenance": {"runner_identity": review["contract"]["reviewer"]["identity"],
                                 "instructions_sha256": review["contract"]["reviewer"]["instructions_sha256"],
                                 "context_sha256": review["review_context"]["sha256"],
                                 "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:01Z"}}
        self.assertEqual(self.worker.api.call("POST", "/worker/report/" + review["attempt_id"], report)["status"],
                         "awaiting_review")
        with connect(self.dsn) as conn:
            evidence = conn.execute("SELECT disposition,result FROM reviews WHERE attempt_id=%s",
                                    (review["attempt_id"],)).fetchone()
            self.assertEqual((evidence["disposition"], evidence["result"]), ("invalid", None))
            self.assertNotEqual(conn.execute("SELECT status FROM runs WHERE id=%s",
                                             (path.split("/")[-1],)).fetchone()["status"], "ready_to_merge")
        replacement = self.claim_and_execute()
        self.assertEqual(replacement["task_id"], review["task_id"])
        self.assertIn(b"Ready to merge", self.browser("GET", path)[2])

    def test_review_input_overflow_blocks_without_dispatch(self):
        with patch("teem.review.MAX_REVIEW_INPUT_BYTES", 600):
            path = self.propose()
            self.claim_and_execute()
            self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
                "capabilities": ["code", "check", "bundle", "review"]})["assignment"])
        self.assertIn(b"review_input_unavailable", self.browser("GET", path)[2])

    def test_blocked_review_and_revision_cap_preserve_evidence(self):
        self.reviewer_verdict("blocked")
        path = self.propose()
        self.claim_and_execute()
        review = self.claim_and_execute()
        with connect(self.dsn) as conn:
            state = conn.execute("SELECT status,stop_reason FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()
            result = conn.execute("SELECT disposition,result FROM reviews WHERE attempt_id=%s",
                                  (review["attempt_id"],)).fetchone()
            self.assertEqual(state, {"status": "blocked", "stop_reason": "review_uncertain"})
            self.assertEqual(result["result"]["verdict"], "blocked")
            self.assertEqual(result["disposition"], "accepted")
        self.reviewer_verdict("changes_required")
        (self.repo / "coder.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "contract=json.loads(Path('/contract.json').read_text())\n"
            "Path('value.txt').write_text('after revised\\n' if contract.get('parent_candidate_id') else 'after\\n')\n")
        (self.repo / "check.py").write_text("from pathlib import Path\nassert Path('value.txt').read_text().startswith('after')\n")
        run("git", "add", ".", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "revision fixture", cwd=self.repo)
        capped = self.propose(revisions=1)
        for _ in range(4):
            self.claim_and_execute()
        with connect(self.dsn) as conn:
            state = conn.execute("SELECT status,stop_reason,current_candidate_id FROM runs WHERE id=%s",
                                 (capped.split("/")[-1],)).fetchone()
            self.assertEqual((state["status"], state["stop_reason"]), ("blocked", "revision_limit"))
            self.assertEqual(conn.execute("SELECT count(*) FROM candidates WHERE run_id=%s",
                                          (capped.split("/")[-1],)).fetchone()["count"], 2)
            self.assertEqual(conn.execute("""SELECT count(*) FROM reviews v JOIN attempts a ON a.id=v.attempt_id
                                             JOIN tasks t ON t.id=a.task_id WHERE t.run_id=%s AND v.disposition='accepted'""",
                                          (capped.split("/")[-1],)).fetchone()["count"], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks WHERE run_id=%s AND status='queued'",
                                          (capped.split("/")[-1],)).fetchone()["count"], 0)

    def test_review_isolation_upload_rejection_and_bundle_tampering(self):
        self.reviewer.write_text(
            "#!/usr/bin/python3\nimport json,socket\nfrom pathlib import Path\n"
            "context=json.loads(Path('/context.json').read_text())\npack=context['pack']\n"
            "assert Path('/workspace/.git').is_dir()\n"
            "assert not Path('/secret-worker-token').exists()\n"
            "try: Path('/context.json').write_text('changed')\n"
            "except OSError: pass\n"
            "else: raise AssertionError('writable context')\n"
            "try: socket.create_connection(('127.0.0.1', 55434), timeout=1)\n"
            "except OSError: pass\n"
            "else: raise AssertionError('network available')\n"
            "print(json.dumps({'candidate_id':pack['candidate_id'],"
            "'contract_version':pack['contract_version'],'context_sha256':context['sha256'],"
            "'verdict':'pass','summary':'Isolated review','findings':[],'uncertainties':[]}))\n")
        self.reviewer.chmod(0o755)
        path = self.propose()
        coding = self.claim_and_execute()
        review = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        with self.assertRaises(WorkerError):
            self.worker.api.call("POST", "/worker/upload/" + review["attempt_id"], b"x",
                                 hashlib.sha256(b"x").hexdigest())
        self.assertTrue(self.worker.journal.accept(review))
        self.worker.execute(review)
        with connect(self.dsn) as conn:
            candidate = conn.execute("SELECT * FROM candidates WHERE attempt_id=%s", (coding["attempt_id"],)).fetchone()
            self.assertEqual(conn.execute("SELECT status FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()["status"],
                             "ready_to_merge")
        bundle = self.root / "artifacts" / (candidate["artifact_sha256"] + ".bundle")
        bundle.write_bytes(b"corrupt")
        self.assertEqual(self.browser("GET", f"/candidates/{candidate['id']}/bundle")[0], 500)
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT status,stop_reason FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone(),
                             {"status": "blocked", "stop_reason": "integrity_failure"})

    def test_review_lease_reconciliation_precedes_retry_and_deadline_blocks(self):
        path = self.propose()
        self.claim_and_execute()
        review = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertTrue(self.worker.journal.accept(review))
        with connect(self.dsn) as conn:
            conn.execute("UPDATE attempts SET lease_until=now()-interval '1 second' WHERE id=%s", (review["attempt_id"],))
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"])
        self.assertEqual(self.browser("POST", path + "/retry", {})[0], 409)
        self.restart_worker()
        self.worker.reconcile()
        self.assertEqual(self.browser("POST", path + "/retry", {})[0], 303)
        replacement = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertEqual(replacement["task_id"], review["task_id"])
        self.assertEqual(replacement["generation"], 2)
        pack = review["review_context"]["pack"]
        old_result = {"outcome": "review", "generation": review["generation"],
            "raw_output": json.dumps({"candidate_id": pack["candidate_id"],
                                      "contract_version": pack["contract_version"],
                                      "context_sha256": review["review_context"]["sha256"],
                                      "verdict": "pass", "summary": "Old judgment", "findings": [], "uncertainties": []}),
            "provenance": {"runner_identity": review["contract"]["reviewer"]["identity"],
                           "instructions_sha256": review["contract"]["reviewer"]["instructions_sha256"],
                           "context_sha256": review["review_context"]["sha256"],
                           "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:01Z"}}
        self.assertEqual(self.worker.api.call("POST", "/worker/report/" + review["attempt_id"],
                                              old_result)["status"], "stale")
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT disposition FROM reviews WHERE attempt_id=%s",
                                          (review["attempt_id"],)).fetchone()["disposition"], "stale")
            self.assertNotEqual(conn.execute("SELECT status FROM runs WHERE id=%s",
                                             (path.split("/")[-1],)).fetchone()["status"], "ready_to_merge")
        with self.assertRaises(WorkerError):
            self.worker.api.call("POST", "/worker/report/" + review["attempt_id"],
                                 {"generation": review["generation"], "outcome": "failed"})
        self.assertTrue(self.worker.journal.accept(replacement))
        self.worker.execute(replacement)
        self.assertIn(b"Ready to merge", self.browser("GET", path)[2])
        other = self.propose()
        self.claim_and_execute()
        self.stop_server()
        with connect(self.dsn) as conn:
            conn.execute("UPDATE runs SET deadline=now()-interval '1 second' WHERE id=%s", (other.split("/")[-1],))
        self.start_server()
        self.worker.api.url = self.url
        self.assertIsNone(self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"])
        self.assertIn(b"deadline", self.browser("GET", other)[2])

    def test_review_timeout_retries_once(self):
        self.reviewer_config["timeout"] = 1
        self.reviewer_file.write_text(json.dumps(self.reviewer_config))
        self.server.app.reviewer = self.reviewer_config
        self.worker.policy["reviewer"]["timeout"] = 1
        self.reviewer.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(10)\n")
        self.reviewer.chmod(0o755)
        path = self.propose()
        self.claim_and_execute()
        first = self.claim_and_execute()
        second = self.claim_and_execute()
        self.assertEqual(first["task_id"], second["task_id"])
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT status FROM runs WHERE id=%s",
                                          (path.split("/")[-1],)).fetchone()["status"], "failed")
            rows = conn.execute("SELECT validation_error FROM reviews WHERE attempt_id IN (%s,%s)",
                                (first["attempt_id"], second["attempt_id"])).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all("reviewer exited" in row["validation_error"] for row in rows))

    def test_failed_revision_keeps_parent_current(self):
        (self.repo / "coder.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            "contract=json.loads(Path('/contract.json').read_text())\n"
            "if contract.get('parent_candidate_id'): sys.exit(1)\n"
            "Path('value.txt').write_text('after\\n')\n")
        run("git", "add", "coder.py", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "failed revision fixture", cwd=self.repo)
        self.reviewer_verdict("changes_required")
        path = self.propose()
        first = self.claim_and_execute()
        self.claim_and_execute()
        failed_one = self.claim_and_execute()
        failed_two = self.claim_and_execute()
        self.assertEqual(failed_one["task_id"], failed_two["task_id"])
        with connect(self.dsn) as conn:
            state = conn.execute("SELECT status,current_candidate_id FROM runs WHERE id=%s",
                                 (path.split("/")[-1],)).fetchone()
            parent = conn.execute("SELECT id FROM candidates WHERE attempt_id=%s", (first["attempt_id"],)).fetchone()
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["current_candidate_id"], parent["id"])
            self.assertEqual(conn.execute("SELECT count(*) FROM candidates WHERE run_id=%s",
                                          (path.split("/")[-1],)).fetchone()["count"], 1)

    def test_revision_failed_checks_preserve_new_candidate_without_review(self):
        (self.repo / "coder.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "contract=json.loads(Path('/contract.json').read_text())\n"
            "Path('value.txt').write_text('bad\\n' if contract.get('parent_candidate_id') else 'after\\n')\n")
        run("git", "add", "coder.py", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "failed check revision", cwd=self.repo)
        self.reviewer_verdict("changes_required")
        path = self.propose(revisions=1)
        self.claim_and_execute()
        self.claim_and_execute()
        revision = self.claim_and_execute()
        self.assertEqual(revision["revision_number"], 1)
        with connect(self.dsn) as conn:
            run_row = conn.execute("SELECT status,current_candidate_id FROM runs WHERE id=%s",
                                   (path.split("/")[-1],)).fetchone()
            candidate = conn.execute("SELECT * FROM candidates WHERE id=%s", (run_row["current_candidate_id"],)).fetchone()
            self.assertEqual(run_row["status"], "checks_failed")
            self.assertNotEqual(candidate["evidence"]["checks"][0]["exit_code"], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks WHERE run_id=%s AND kind='review'",
                                          (path.split("/")[-1],)).fetchone()["count"], 1)

    def test_attempt_cap_across_roles_blocks_next_revision(self):
        (self.repo / "coder.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "contract=json.loads(Path('/contract.json').read_text())\n"
            "Path('value.txt').write_text('after revised\\n' if contract.get('parent_candidate_id') else 'after\\n')\n")
        (self.repo / "check.py").write_text("from pathlib import Path\nassert Path('value.txt').read_text().startswith('after')\n")
        run("git", "add", ".", cwd=self.repo)
        run("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "attempt cap fixture", cwd=self.repo)
        self.reviewer_verdict("changes_required")
        path = self.propose(revisions=2)

        def fail_attempt():
            attempt = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
                "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
            self.assertIsNotNone(attempt)
            self.worker.api.call("POST", "/worker/report/" + attempt["attempt_id"],
                                 {"generation": attempt["generation"], "outcome": "failed",
                                  "usage": {"error": "isolated execution failed"}})
            return attempt

        fail_attempt()
        self.claim_and_execute()
        fail_attempt()
        self.claim_and_execute()
        fail_attempt()
        self.claim_and_execute()
        fail_attempt()
        last = self.claim_and_execute()
        self.assertEqual(last["kind"], "review")
        with connect(self.dsn) as conn:
            run_row = conn.execute("SELECT status,stop_reason FROM runs WHERE id=%s", (path.split("/")[-1],)).fetchone()
            self.assertEqual(run_row, {"status": "blocked", "stop_reason": "attempt_limit"})
            self.assertEqual(conn.execute("""SELECT count(*) FROM attempts a JOIN tasks t ON t.id=a.task_id
                                             WHERE t.run_id=%s""", (path.split("/")[-1],)).fetchone()["count"], 8)
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks WHERE run_id=%s AND revision_number=2",
                                          (path.split("/")[-1],)).fetchone()["count"], 0)

    def test_cancellation_before_review_result_prevents_revision(self):
        self.reviewer_verdict("changes_required")
        path = self.propose()
        self.claim_and_execute()
        review = self.worker.api.call("POST", "/worker/claim", {"worker_id": "worker",
            "capabilities": ["code", "check", "bundle", "review"]})["assignment"]
        self.assertEqual(self.browser("POST", path + "/cancel", {})[0], 303)
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT status FROM runs WHERE id=%s",
                                          (path.split("/")[-1],)).fetchone()["status"], "cancelling")
        self.assertEqual(self.browser("POST", "/requests", {"project": self.project_id,
            "objective": "Another", "criteria": "Another"})[0], 409)
        pack = review["review_context"]["pack"]
        report = {"outcome": "review", "generation": review["generation"],
                  "raw_output": json.dumps({"candidate_id": pack["candidate_id"],
                                            "contract_version": pack["contract_version"],
                                            "context_sha256": review["review_context"]["sha256"],
                                            "verdict": "changes_required", "summary": "Needs change",
                                            "findings": [{"criterion": pack["acceptance_criteria"],
                                                          "description": "Change value",
                                                          "evidence": [{"kind": "source", "path": "value.txt",
                                                                        "start_line": 1, "end_line": 1}]}],
                                            "uncertainties": []}),
                  "provenance": {"runner_identity": review["contract"]["reviewer"]["identity"],
                                 "instructions_sha256": review["contract"]["reviewer"]["instructions_sha256"],
                                 "context_sha256": review["review_context"]["sha256"],
                                 "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:01Z"}}
        self.assertEqual(self.worker.api.call("POST", "/worker/report/" + review["attempt_id"], report)["status"],
                         "cancelled")
        with connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT disposition FROM reviews WHERE attempt_id=%s",
                                          (review["attempt_id"],)).fetchone()["disposition"], "stale")
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks WHERE run_id=%s AND revision_number=1",
                                          (path.split("/")[-1],)).fetchone()["count"], 0)


if __name__ == "__main__":
    unittest.main()
