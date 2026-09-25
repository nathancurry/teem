import argparse
import base64
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .common import (CHECK_SECONDS, CODER_SECONDS, COMMIT_RE, MAX_OUTPUT_BYTES, PROTOCOL, REPO_RE,
                     canonical, check_config, digest)


class WorkerError(Exception):
    pass


class LeaseLost(WorkerError):
    pass


class WorkerCancelled(WorkerError):
    pass


def valid_role_extras(role):
    env, home = role.get("env", {}), role.get("home")
    return isinstance(env, dict) and all(isinstance(k, str) and k and isinstance(v, str) for k, v in env.items()) and \
        (home is None or isinstance(home, str) and Path(home).is_absolute() and Path(home).is_dir())


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


def git(*args, cwd=None, env=None, timeout=30):
    safe_env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null"}
    if env:
        safe_env.update(env)
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=safe_env, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise WorkerError(f"git {' '.join(args[:2])} timed out") from None
    if result.returncode:
        raise WorkerError(f"git {' '.join(args[:2])} failed: {result.stderr[-1000:]}")
    return result.stdout.strip()


def podman_env():
    # Rootless Podman needs the user's runtime directory; containers get only explicit --env values.
    return {key: os.environ[key] for key in ("PATH", "HOME", "XDG_RUNTIME_DIR", "USER") if key in os.environ}


def container_command(policy, name, attempt_id, argv, mounts, workdir, timeout, role=None):
    """One disposable container whose only network path is the allowlisting proxy.

    A role (the coder or reviewer policy) may add its model credential as environment and a
    persistent home directory; checks get neither.
    """
    role = role or {}
    mounts = list(mounts)
    home = "/tmp"
    if role.get("home"):
        mounts.append((role["home"], "/home/agent", "rw"))
        home = "/home/agent"
    command = ["podman", "run", "--rm", "--name", name, "--label", "teem.attempt=" + attempt_id,
               "--userns=keep-id", "--read-only", "--tmpfs", "/tmp:rw,exec,size=2g",
               "--pids-limit", "512", "--memory", "6g", "--cpus", "4",
               # Bounds a container orphaned by a crashed worker until the next worker start removes it.
               "--timeout", str(int(timeout) + 60),
               "--network", policy["network"], "--workdir", workdir,
               "--env", "HOME=" + home, "--env", "HTTPS_PROXY=" + policy["proxy"],
               "--env", "HTTP_PROXY=" + policy["proxy"], "--env", "NO_PROXY=localhost,127.0.0.1",
               "--env", "TEEM_CONTRACT=/contract.json"]
    for key, value in role.get("env", {}).items():
        command += ["--env", f"{key}={value}"]
    for source, target, mode in mounts:
        command += ["-v", f"{Path(source).resolve()}:{target}:{mode},z"]
    return command + [policy["image"], *argv]


def remove_containers(attempt_id):
    subprocess.run(["podman", "rm", "--force", "--time", "0", "--filter", "label=teem.attempt=" + attempt_id],
                   env=podman_env(), capture_output=True, timeout=60)


def restricted_run(command, attempt_id, timeout, heartbeat, journal=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=podman_env(), start_new_session=True)
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
            # Stop the client first so it cannot create the container after removal runs.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            remove_containers(attempt_id)
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
        self.policy = json.loads(Path(args.projects).read_text())
        required = {"owners", "coder", "reviewer", "image", "network", "proxy"}
        if not isinstance(self.policy, dict) or not required <= set(self.policy) or \
           not set(self.policy) <= required | {"git_base", "token"}:
            raise WorkerError("worker policy needs owners, coder, reviewer, image, network, and proxy, "
                              "with optional git_base and token")
        if any(not isinstance(self.policy[key], str) or not self.policy[key] for key in ("image", "network", "proxy")) or \
           not self.policy["proxy"].startswith("http://"):
            raise WorkerError("image, network, and an http:// proxy URL are required")
        if not isinstance(self.policy["owners"], list) or not self.policy["owners"] or \
           any(not isinstance(o, str) or not REPO_RE.fullmatch(o + "/x") for o in self.policy["owners"]):
            raise WorkerError("owners must be lowercase GitHub owner names")
        coder = self.policy["coder"]
        if not isinstance(coder, dict) or not {"argv"} <= set(coder) <= {"argv", "env", "home"} or \
           not isinstance(coder["argv"], list) or not coder["argv"] or \
           any(not isinstance(item, str) or not item for item in coder["argv"]) or not valid_role_extras(coder):
            raise WorkerError("coder needs an argv list, with optional env and home")
        reviewer = self.policy["reviewer"]
        required = {"identity", "instructions_sha256", "destination", "timeout", "executable"}
        if not required <= set(reviewer) <= required | {"env", "home"} or \
           reviewer["destination"] not in ("local", "openai") or not valid_role_extras(reviewer) or \
           not Path(reviewer["executable"]).is_absolute() or \
           not Path(reviewer["executable"]).is_file():
            raise WorkerError("reviewer must be a worker-installed executable")
        self.git_base = self.policy.get("git_base", "https://github.com")
        (self.state_dir / "mirrors").mkdir(exist_ok=True, mode=0o700)

    def mirror(self, repo):
        """Fetch the worker's own copy of a repository; workspaces clone from it."""
        path = self.state_dir / "mirrors" / (repo.replace("/", "__") + ".git")
        env = {}
        if self.policy.get("token"):
            # The read-only token stays in the supervisor's git environment, never in a sandbox.
            basic = base64.b64encode(("x-access-token:" + self.policy["token"]).encode()).decode()
            env = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "http.extraHeader",
                   "GIT_CONFIG_VALUE_1": "Authorization: Basic " + basic}
        if path.exists():
            git("fetch", "--prune", "origin", cwd=path, env=env, timeout=300)
        else:
            git("clone", "--mirror", "--quiet", f"{self.git_base}/{repo}.git", str(path), env=env, timeout=600)
        return path

    def repo_checks(self, mirror, base):
        try:
            git("cat-file", "-e", base + ":.teem/checks.json", cwd=mirror)
        except WorkerError:
            return []
        return check_config(json.loads(git("show", base + ":.teem/checks.json", cwd=mirror)))

    def remaining(self, assignment, limit):
        deadline = datetime.fromisoformat(assignment["deadline"])
        seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
        if seconds <= 0:
            raise WorkerError("run deadline exceeded")
        return min(limit, seconds)

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
            if state in ("preparing", "executing"):
                # Execution is single-threaded, so an unfinished attempt here belongs to a previous
                # worker process. Its containers outlived it; stop them before reconciling.
                remove_containers(attempt_id)
            assignment = json.loads(assignment_json)
            result = json.loads(result_json) if result_json else None
            if result and result["outcome"] == "candidate":
                bundle = Path(result["bundle"])
                try:
                    intact = hashlib.sha256(bundle.read_bytes()).hexdigest() == result["artifact_sha256"]
                except OSError:
                    intact = False
                if not intact:
                    if datetime.fromisoformat(assignment["deadline"]) > datetime.now(timezone.utc):
                        result = {"outcome": "uncertain", "generation": generation}
                        self.journal.update(attempt_id, "ready_to_report", result=result)
            try:
                response = self.api.call("POST", "/worker/reconcile/" + attempt_id,
                                         {"generation": generation, "journal_state": state,
                                          "outcome": result["outcome"] if result else None})
            except (WorkerError, urllib.error.URLError):
                continue
            if response["status"] in ("superseded", "candidate", "review", "failed", "cancelled", "reconciled_uncertain", "deadline"):
                self.journal.update(attempt_id, "done")
                continue
            if result:
                try:
                    self.send_result(assignment, result)
                except (WorkerError, urllib.error.URLError):
                    continue

    def execute(self, assignment):
        attempt_id = assignment["attempt_id"]
        project_id = assignment["project_id"]
        if not REPO_RE.fullmatch(project_id) or project_id.split("/")[0] not in self.policy["owners"]:
            self.report_failure(assignment, "unconfigured project", policy=True)
            return
        contract = assignment["contract"]
        if contract["project_id"] != project_id or \
           contract["allowed_actions"] != ["code", "check", "review", "revise", "publish_pr"] or \
           {k: v for k, v in self.policy["reviewer"].items() if k not in ("executable", "env", "home")} != contract["reviewer"]:
            self.report_failure(assignment, "worker-local policy does not allow assignment", policy=True)
            return
        if assignment["kind"] == "review":
            self.execute_review(assignment)
            return
        base = contract["base_commit"]
        if not COMMIT_RE.fullmatch(base):
            self.report_failure(assignment, "invalid base commit", policy=True)
            return
        try:
            repo = self.mirror(project_id)
            checks = self.repo_checks(repo, base)
        except (WorkerError, ValueError) as exc:
            self.report_failure(assignment, "repository unavailable: " + str(exc)[:500])
            return
        # The server read the checks at the same base; a mismatch means the contract is not what the repository declares.
        if checks != contract["checks"] or digest(checks) != contract["check_hash"]:
            self.report_failure(assignment, "worker-local policy does not allow assignment", policy=True)
            return
        workspace = self.state_dir / ("attempt-" + attempt_id)
        self.journal.update(attempt_id, "preparing", workspace=str(workspace))

        def ensure_lease():
            self.heartbeat(assignment)

        try:
            if workspace.exists():
                raise WorkerError("attempt workspace already exists")
            ensure_lease()
            if assignment["input_candidate_id"]:
                bundle_data = self.api.call("GET", "/worker/bundle/" + assignment["input_candidate_id"])
                if hashlib.sha256(bundle_data).hexdigest() != assignment["input_artifact_sha256"]:
                    raise WorkerError("parent bundle integrity failure")
                parent_bundle = self.state_dir / (attempt_id + ".parent.bundle")
                parent_bundle.write_bytes(bundle_data)
                git("clone", "--no-hardlinks", str(parent_bundle), str(workspace))
                start_head = assignment["input_head"]
            else:
                git("clone", "--local", "--no-hardlinks", str(repo), str(workspace))
                start_head = base
            ensure_lease()
            git("checkout", "--detach", start_head, cwd=workspace)
            ensure_lease()
            if git("rev-parse", "HEAD", cwd=workspace) != start_head:
                raise WorkerError("starting commit mismatch")
            contract_file = self.state_dir / (attempt_id + ".json")
            coding_input = dict(contract)
            if assignment["input_candidate_id"]:
                coding_input["parent_candidate_id"] = assignment["input_candidate_id"]
                coding_input["accepted_review"] = assignment["source_review"]
                coding_input["parent_check_evidence"] = assignment.get("parent_check_evidence")
            contract_file.write_text(canonical(coding_input))
            with contract_file.open("rb") as f:
                os.fsync(f.fileno())
            timeout = self.remaining(assignment, CODER_SECONDS)
            mounts = [(workspace, "/workspace", "rw"), (workspace / ".git", "/workspace/.git", "ro"),
                      (contract_file, "/contract.json", "ro")]
            code, output = restricted_run(
                container_command(self.policy, "teem-" + attempt_id, attempt_id, self.policy["coder"]["argv"],
                                  mounts, "/workspace", timeout, self.policy["coder"]),
                attempt_id, timeout, lambda: self.heartbeat(assignment), self.journal)
            if code:
                raise WorkerError("coder exited " + str(code) + ": " + output[-1000:])
            summary = output.strip()[-4000:]
            ensure_lease()
            git("add", "-A", cwd=workspace)
            ensure_lease()
            env = {"GIT_AUTHOR_NAME": "Teem Worker", "GIT_AUTHOR_EMAIL": "teem@localhost",
                   "GIT_COMMITTER_NAME": "Teem Worker", "GIT_COMMITTER_EMAIL": "teem@localhost"}
            staged = git("diff", "--cached", "--name-only", cwd=workspace)
            if not staged:
                # An implementer that changes nothing is asking something; its last output is the question.
                self.report_outcome(assignment, "failed", summary or "The implementer made no changes.",
                                    kind="needs_input")
                return
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
            for index, check in enumerate(checks):
                ensure_lease()
                check_workspace = self.state_dir / f"check-{attempt_id}-{index}"
                git("clone", "--local", "--no-hardlinks", str(workspace), str(check_workspace))
                ensure_lease()
                git("checkout", "--detach", head, cwd=check_workspace)
                ensure_lease()
                timeout = self.remaining(assignment, CHECK_SECONDS)
                mounts = [(check_workspace, "/workspace", "rw"), (check_workspace / ".git", "/workspace/.git", "ro"),
                          (contract_file, "/contract.json", "ro")]
                code, output = restricted_run(
                    container_command(self.policy, f"teem-{attempt_id}-check{index}", attempt_id, check["argv"],
                                      mounts, "/workspace", timeout),
                    attempt_id, timeout, lambda: self.heartbeat(assignment))
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
                      "usage": {"coder_seconds_limit": CODER_SECONDS, "check_seconds_limit": CHECK_SECONDS,
                                "summary": summary}}
            self.journal.update(attempt_id, "ready_to_report", result=result)
        except LeaseLost:
            self.report_outcome(assignment, "uncertain")
            return
        except WorkerCancelled:
            self.report_outcome(assignment, "cancelled")
            return
        except Exception as exc:
            reason = str(exc)
            self.report_failure(assignment, reason,
                                policy="bundle integrity" in reason or "starting commit mismatch" in reason)
            return
        try:
            self.send_result(assignment, result)
        except (WorkerError, urllib.error.URLError):
            # The candidate and its checks remain in the journal for reconciliation.
            pass

    def execute_review(self, assignment):
        reviewer = self.policy["reviewer"]
        attempt_id = assignment["attempt_id"]
        context = assignment["review_context"]
        if not context or digest(context["pack"]) != context["sha256"] or \
           context["pack"]["candidate_id"] != assignment["input_candidate_id"]:
            self.report_failure(assignment, "review context identity mismatch", policy=True)
            return
        scratch = self.state_dir / ("review-" + attempt_id)
        context_file = self.state_dir / (attempt_id + ".context.json")
        provenance = {"runner_identity": reviewer["identity"],
                      "instructions_sha256": reviewer["instructions_sha256"],
                      "context_sha256": context["sha256"], "started_at": datetime.now(timezone.utc).isoformat(),
                      "ended_at": None}
        try:
            scratch.mkdir(mode=0o700)
            context_file.write_text(canonical(context))
            with context_file.open("rb") as f:
                os.fsync(f.fileno())
            self.heartbeat(assignment)
            # The reviewer works in its own disposable checkout of the exact Candidate it judges.
            bundle_data = self.api.call("GET", "/worker/bundle/" + assignment["input_candidate_id"])
            if hashlib.sha256(bundle_data).hexdigest() != context["pack"]["bundle_sha256"]:
                raise WorkerError("candidate bundle integrity failure")
            bundle = self.state_dir / (attempt_id + ".review.bundle")
            bundle.write_bytes(bundle_data)
            checkout = self.state_dir / ("review-checkout-" + attempt_id)
            git("clone", "--quiet", "--no-hardlinks", str(bundle), str(checkout))
            git("checkout", "--quiet", "--detach", context["pack"]["head_commit"], cwd=checkout)
            self.heartbeat(assignment)
            # A private copy keeps the installed executable's file label and contents untouched.
            runner = self.state_dir / (attempt_id + ".runner")
            shutil.copyfile(reviewer["executable"], runner)
            runner.chmod(0o500)
            timeout = self.remaining(assignment, reviewer["timeout"])
            mounts = [(scratch, "/scratch", "rw"), (context_file, "/context.json", "ro"), (runner, "/runner", "ro"),
                      (checkout, "/workspace", "rw"), (checkout / ".git", "/workspace/.git", "ro")]
            code, output = restricted_run(
                container_command(self.policy, "teem-" + attempt_id, attempt_id, ["/runner"],
                                  mounts, "/workspace", timeout, reviewer),
                attempt_id, timeout, lambda: self.heartbeat(assignment), self.journal)
            provenance["ended_at"] = datetime.now(timezone.utc).isoformat()
            result = {"outcome": "review", "generation": assignment["generation"],
                      "raw_output": output, "provenance": provenance}
            if code:
                result["validation_error"] = "reviewer exited " + str(code)
            elif len(output.encode()) >= MAX_OUTPUT_BYTES:
                result["validation_error"] = "review output exceeds cap"
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
            pass

    def report_failure(self, assignment, reason, policy=False):
        if assignment["kind"] == "review" and not policy:
            reviewer = assignment["contract"]["reviewer"]
            context = assignment.get("review_context") or {}
            now = datetime.now(timezone.utc).isoformat()
            result = {"outcome": "review", "generation": assignment["generation"],
                      "raw_output": "", "validation_error": reason[:1000],
                      "provenance": {"runner_identity": reviewer["identity"],
                                     "instructions_sha256": reviewer["instructions_sha256"],
                                     "context_sha256": context.get("sha256"),
                                     "started_at": now, "ended_at": now}}
            self.journal.update(assignment["attempt_id"], "ready_to_report", result=result)
            try:
                self.send_result(assignment, result)
            except (WorkerError, urllib.error.URLError):
                pass
            return
        self.report_outcome(assignment, "failed", reason, "policy" if policy else None)

    def report_outcome(self, assignment, outcome, reason=None, kind=None):
        result = {"outcome": outcome, "generation": assignment["generation"],
                  "usage": {"error": reason[:1000]} if reason else {}}
        if kind:
            result["failure_kind"] = kind
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
                if datetime.fromisoformat(assignment["deadline"]) > datetime.now(timezone.utc):
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
                                         {"worker_id": self.worker_id, "capabilities": ["code", "check", "bundle", "review"]})
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
    parser.add_argument("--projects", required=True,
                        help="worker-local policy JSON: allowed GitHub owners, coder, reviewer, optional read token")
    parser.add_argument("--state-dir", required=True)
    Worker(parser.parse_args()).run()


if __name__ == "__main__":
    main()
