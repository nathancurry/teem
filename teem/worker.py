import argparse
import hashlib
import json
import os
import resource
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import (CHECK_SECONDS, CODER_SECONDS, COMMIT_RE, MAX_OUTPUT_BYTES, PROTOCOL,
                     canonical, check_config, digest)


class WorkerError(Exception):
    pass


class LeaseLost(WorkerError):
    pass


class WorkerCancelled(WorkerError):
    pass


class Api:
    def __init__(self, url, token):
        if not url.startswith("https://"):
            raise WorkerError("worker server URL must use HTTPS")
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method, path, data=None, sha=None):
        headers = {"Authorization": "Bearer " + self.token, "X-Teem-Protocol": str(PROTOCOL)}
        if isinstance(data, (dict, list)):
            data = canonical(data).encode()
            headers["Content-Type"] = "application/json"
        if sha:
            headers["X-Artifact-SHA256"] = sha
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read()) if response.headers.get_content_type() == "application/json" else response.read()
        except urllib.error.HTTPError as exc:
            raise WorkerError(f"server returned {exc.code}: {exc.read(500).decode(errors='replace')}") from exc


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS attempts (
            id TEXT PRIMARY KEY, generation INTEGER NOT NULL, assignment TEXT NOT NULL,
            state TEXT NOT NULL, workspace TEXT, pid INTEGER, result TEXT)""")
        self.db.commit()

    def accept(self, assignment):
        attempt_id = assignment["attempt_id"]
        row = self.db.execute("SELECT assignment FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row:
            if json.loads(row[0]) != assignment:
                raise WorkerError("duplicate attempt has changed assignment")
            return False
        self.db.execute("INSERT INTO attempts(id,generation,assignment,state) VALUES (?,?,?,'accepted')",
                        (attempt_id, assignment["generation"], canonical(assignment)))
        self.db.commit()
        return True

    def update(self, attempt_id, state, workspace=None, pid=None, result=None):
        self.db.execute("UPDATE attempts SET state=?,workspace=COALESCE(?,workspace),pid=?,result=COALESCE(?,result) WHERE id=?",
                        (state, workspace, pid, canonical(result) if result is not None else None, attempt_id))
        self.db.commit()

    def pending(self):
        return self.db.execute("SELECT id,generation,assignment,state,workspace,pid,result FROM attempts WHERE state!='done'").fetchall()


def git(*args, cwd=None, env=None):
    safe_env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null"}
    if env:
        safe_env.update(env)
    result = subprocess.run(["git", *args], cwd=cwd, env=safe_env, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise WorkerError(f"git {' '.join(args[:2])} failed: {result.stderr[-1000:]}")
    return result.stdout.strip()


def sandbox_command(argv, workspace, contract_file):
    workspace = Path(workspace).resolve()
    contract_file = Path(contract_file).resolve()
    command = ["bwrap", "--unshare-all", "--new-session", "--die-with-parent",
               "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
               "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
               "--bind", str(workspace), "/workspace",
               "--ro-bind", str(workspace / ".git"), "/workspace/.git",
               "--ro-bind", str(contract_file), "/contract.json",
               "--chdir", "/workspace", "--setenv", "HOME", "/tmp",
               "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "TEEM_CONTRACT", "/contract.json",
               "--", "/bin/bash", "-c", "ulimit -u 64 && ulimit -H -u 64 && exec \"$@\"", "teem-exec"]
    return command + argv


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (CODER_SECONDS + 10, CODER_SECONDS + 10))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    # Bubblewrap needs a higher limit while creating namespaces; the shell inside lowers it before coder code runs.
    resource.setrlimit(resource.RLIMIT_NPROC, (1024, 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setsid()


def restricted_run(argv, workspace, contract_file, timeout, heartbeat, journal=None, attempt_id=None):
    process = subprocess.Popen(sandbox_command(argv, workspace, contract_file),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env={"PATH": "/usr/bin:/bin"}, preexec_fn=limits)
    reader_started = False
    try:
        output = bytearray()
        done = threading.Event()

        def read_output():
            while True:
                block = process.stdout.read(4096)
                if not block:
                    break
                if len(output) < MAX_OUTPUT_BYTES:
                    output.extend(block[:MAX_OUTPUT_BYTES - len(output)])
            done.set()

        if journal:
            journal.update(attempt_id, "executing", pid=process.pid)
        thread = threading.Thread(target=read_output, daemon=True)
        thread.start()
        reader_started = True
        deadline = time.monotonic() + timeout
        next_renew = time.monotonic() + 10
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                break
            if now >= next_renew:
                heartbeat()
                next_renew = now + 10
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        if reader_started:
            done.wait(timeout=2)
        process.stdout.close()
    return process.returncode, output.decode(errors="replace")


class Worker:
    def __init__(self, args):
        self.api = Api(args.url, args.token)
        self.worker_id = args.worker_id
        self.state_dir = Path(args.state_dir).resolve()
        self.state_dir.mkdir(exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.journal = Journal(self.state_dir / "journal.sqlite3")
        for directory in (self.state_dir, self.state_dir.parent):
            fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self.projects = json.loads(Path(args.projects).read_text())
        if not isinstance(self.projects, dict):
            raise WorkerError("projects must be a JSON object")
        for project in self.projects.values():
            if set(project) != {"repo", "coder", "checks"}:
                raise WorkerError("project needs repo, coder, and checks")
            check_config(project["checks"])
            if not isinstance(project["coder"], list) or not project["coder"]:
                raise WorkerError("coder command required")
            if not Path(project["repo"]).is_absolute():
                raise WorkerError("worker repository paths must be absolute")

    def heartbeat(self, assignment):
        try:
            result = self.api.call("POST", "/worker/renew/" + assignment["attempt_id"],
                                   {"generation": assignment["generation"]})
        except (WorkerError, urllib.error.URLError) as exc:
            raise LeaseLost("attempt renewal failed") from exc
        if result["cancel"]:
            raise WorkerCancelled("attempt cancelled")

    def reconcile(self):
        for attempt_id, generation, assignment_json, state, workspace, pid, result_json in self.journal.pending():
            if state == "executing" and pid:
                # A restarted worker cannot settle the attempt while the old sandbox process may still be alive.
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    continue
                else:
                    continue
            assignment = json.loads(assignment_json)
            result = json.loads(result_json) if result_json else None
            if result and result["outcome"] == "candidate":
                bundle = Path(result["bundle"])
                try:
                    intact = hashlib.sha256(bundle.read_bytes()).hexdigest() == result["artifact_sha256"]
                except OSError:
                    intact = False
                if not intact:
                    result = {"outcome": "uncertain", "generation": generation}
                    self.journal.update(attempt_id, "ready_to_report", result=result)
            try:
                response = self.api.call("POST", "/worker/reconcile/" + attempt_id,
                                         {"generation": generation, "journal_state": state,
                                          "outcome": result["outcome"] if result else None})
            except (WorkerError, urllib.error.URLError):
                continue
            if response["status"] in ("superseded", "candidate", "failed", "cancelled", "reconciled_uncertain", "deadline"):
                self.journal.update(attempt_id, "done")
                continue
            if result:
                if response["cancel"]:
                    result = {"outcome": "cancelled", "generation": generation}
                    self.journal.update(attempt_id, "ready_to_report", result=result)
                try:
                    self.send_result(assignment, result)
                except (WorkerError, urllib.error.URLError):
                    continue

    def execute(self, assignment):
        attempt_id = assignment["attempt_id"]
        project = self.projects.get(assignment["project_id"])
        if not project:
            self.report_failure(assignment, "unconfigured project")
            return
        contract = assignment["contract"]
        if contract["project_id"] != assignment["project_id"] or contract["allowed_actions"] != ["code", "check"] or \
           digest(project["checks"]) != contract["check_hash"] or project["checks"] != contract["checks"]:
            self.report_failure(assignment, "worker-local policy does not allow assignment")
            return
        repo = Path(project["repo"])
        base = contract["base_commit"]
        if not COMMIT_RE.fullmatch(base):
            self.report_failure(assignment, "invalid base commit")
            return
        workspace = self.state_dir / ("attempt-" + attempt_id)
        self.journal.update(attempt_id, "preparing", workspace=str(workspace))

        def ensure_lease():
            self.heartbeat(assignment)

        try:
            if workspace.exists():
                raise WorkerError("attempt workspace already exists")
            ensure_lease()
            git("clone", "--local", "--no-hardlinks", str(repo), str(workspace))
            ensure_lease()
            git("checkout", "--detach", base, cwd=workspace)
            ensure_lease()
            if git("rev-parse", "HEAD", cwd=workspace) != base:
                raise WorkerError("base commit mismatch")
            contract_file = self.state_dir / (attempt_id + ".json")
            contract_file.write_text(canonical(contract))
            with contract_file.open("rb") as f:
                os.fsync(f.fileno())
            code, output = restricted_run(project["coder"], workspace, contract_file, CODER_SECONDS,
                                          lambda: self.heartbeat(assignment), self.journal, attempt_id)
            if code:
                raise WorkerError("coder exited " + str(code) + ": " + output[-1000:])
            ensure_lease()
            git("add", "-A", cwd=workspace)
            ensure_lease()
            env = {"GIT_AUTHOR_NAME": "Teem Worker", "GIT_AUTHOR_EMAIL": "teem@localhost",
                   "GIT_COMMITTER_NAME": "Teem Worker", "GIT_COMMITTER_EMAIL": "teem@localhost"}
            staged = git("diff", "--cached", "--name-only", cwd=workspace)
            if not staged:
                raise WorkerError("coder produced no code changes")
            git("commit", "-m", "Candidate for " + attempt_id, cwd=workspace, env=env)
            ensure_lease()
            head = git("rev-parse", "HEAD", cwd=workspace)
            bundle = self.state_dir / (attempt_id + ".bundle")
            git("bundle", "create", str(bundle), "HEAD", cwd=workspace)
            with bundle.open("rb") as f:
                os.fsync(f.fileno())
            dir_fd = os.open(self.state_dir, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            ensure_lease()
            bundle_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
            results = []
            for index, check in enumerate(project["checks"]):
                ensure_lease()
                check_workspace = self.state_dir / f"check-{attempt_id}-{index}"
                git("clone", "--local", "--no-hardlinks", str(workspace), str(check_workspace))
                ensure_lease()
                git("checkout", "--detach", head, cwd=check_workspace)
                ensure_lease()
                code, output = restricted_run(check["argv"], check_workspace, contract_file, CHECK_SECONDS,
                                              lambda: self.heartbeat(assignment))
                results.append({"name": check["name"], "argv": check["argv"],
                                "exit_code": code, "output": output})
                if git("rev-parse", "HEAD", cwd=workspace) != head:
                    raise WorkerError("candidate revision changed during checks")
            result = {"outcome": "candidate", "generation": assignment["generation"],
                      "base_commit": base, "head_commit": head,
                      "contract_version": assignment["contract_version"], "artifact_sha256": bundle_sha,
                      "evidence": {"base_commit": base, "head_commit": head,
                                   "contract_version": assignment["contract_version"],
                                   "check_hash": contract["check_hash"], "sandbox": "bubblewrap-0.11",
                                   "checks": results}, "bundle": str(bundle),
                      "usage": {"coder_seconds_limit": CODER_SECONDS, "check_seconds_limit": CHECK_SECONDS}}
            self.journal.update(attempt_id, "ready_to_report", result=result)
        except LeaseLost:
            self.report_outcome(assignment, "uncertain")
            return
        except WorkerCancelled:
            self.report_outcome(assignment, "cancelled")
            return
        except Exception as exc:
            self.report_failure(assignment, str(exc))
            return
        try:
            self.send_result(assignment, result)
        except (WorkerError, urllib.error.URLError):
            # The candidate and its checks remain in the journal for reconciliation.
            pass

    def report_failure(self, assignment, reason):
        self.report_outcome(assignment, "failed", reason)

    def report_outcome(self, assignment, outcome, reason=None):
        result = {"outcome": outcome, "generation": assignment["generation"],
                  "usage": {"error": reason[:1000]} if reason else {}}
        self.journal.update(assignment["attempt_id"], "ready_to_report", result=result)
        try:
            self.send_result(assignment, result)
        except (WorkerError, urllib.error.URLError):
            pass

    def send_result(self, assignment, result):
        attempt_id = assignment["attempt_id"]
        if result["outcome"] == "candidate":
            bundle = Path(result["bundle"])
            try:
                data = bundle.read_bytes()
            except OSError as exc:
                self.journal.update(attempt_id, "ready_to_report",
                                    result={"outcome": "uncertain", "generation": assignment["generation"]})
                raise WorkerError("candidate bundle unavailable") from exc
            self.api.call("POST", "/worker/upload/" + attempt_id, data, result["artifact_sha256"])
        report = {key: value for key, value in result.items() if key != "bundle"}
        self.api.call("POST", "/worker/report/" + attempt_id, report)
        self.journal.update(attempt_id, "done")

    def run(self):
        while True:
            try:
                self.reconcile()
                response = self.api.call("POST", "/worker/claim",
                                         {"worker_id": self.worker_id, "capabilities": ["code", "check", "bundle"]})
                assignment = response["assignment"]
                if assignment and self.journal.accept(assignment):
                    self.execute(assignment)
            except (WorkerError, urllib.error.URLError) as exc:
                print(f"worker: {exc}", flush=True)
            time.sleep(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--projects", required=True, help="worker-local JSON project policy")
    parser.add_argument("--state-dir", required=True)
    Worker(parser.parse_args()).run()


if __name__ == "__main__":
    main()
