import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg

from .common import (
    COMMIT_RE,
    LEASE_SECONDS,
    MAX_ARTIFACT_BYTES,
    PROTOCOL,
    RUN_SECONDS,
    canonical,
    check_config,
    digest,
    new_id,
)
from .db import connect, event, initialize


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def fail(status, message):
    raise ApiError(status, message)


def lock_attempt_rows(conn, attempt_id):
    identity = conn.execute("""SELECT t.run_id,a.task_id FROM attempts a
                               JOIN tasks t ON t.id=a.task_id WHERE a.id=%s""", (attempt_id,)).fetchone()
    if not identity:
        return None
    # Every transition that locks these records takes Run, then Task, then Attempt.
    conn.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (identity["run_id"],))
    conn.execute("SELECT id FROM tasks WHERE id=%s FOR UPDATE", (identity["task_id"],))
    return conn.execute("""SELECT a.*,t.run_id,t.cancel_requested,t.generation AS task_generation,
                          r.contract_version,r.status AS run_status,
                          r.deadline,c.body FROM attempts a JOIN tasks t ON t.id=a.task_id
                          JOIN runs r ON r.id=t.run_id JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                          WHERE a.id=%s FOR UPDATE OF a""", (attempt_id,)).fetchone()


def expire_leases(conn):
    queued = conn.execute("SELECT id FROM runs WHERE status='queued' AND deadline<now() ORDER BY id").fetchall()
    for candidate in queued:
        run = conn.execute("SELECT status,deadline FROM runs WHERE id=%s FOR UPDATE", (candidate["id"],)).fetchone()
        if run["status"] != "queued" or run["deadline"] >= datetime.now(timezone.utc):
            continue
        conn.execute("UPDATE runs SET status='failed',updated_at=now() WHERE id=%s", (candidate["id"],))
        conn.execute("UPDATE tasks SET status='failed' WHERE run_id=%s AND status='queued'", (candidate["id"],))
        event(conn, candidate["id"], "deadline_exceeded", {})
    rows = conn.execute("""SELECT a.id FROM attempts a JOIN tasks t ON t.id=a.task_id
                           JOIN runs r ON r.id=t.run_id WHERE a.status='running'
                           AND (a.lease_until < now() OR r.deadline < now())
                           ORDER BY r.id,t.id,a.id""").fetchall()
    for candidate in rows:
        row = lock_attempt_rows(conn, candidate["id"])
        if not row:
            continue
        now = datetime.now(timezone.utc)
        if row["status"] != "running" or (row["lease_until"] >= now and row["deadline"] >= now):
            continue
        conn.execute("UPDATE attempts SET status='uncertain' WHERE id=%s", (row["id"],))
        status = "cancelling" if row["cancel_requested"] else "uncertain"
        conn.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, row["task_id"]))
        conn.execute("UPDATE runs SET status=%s, updated_at=now() WHERE id=%s", (status, row["run_id"]))
        kind = "deadline_exceeded" if row["deadline"] < now else "lease_expired"
        event(conn, row["run_id"], kind, {"attempt_id": str(row["id"])})


def page(title, content):
    return ("<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='theme-color' content='#172337'>"
            "<link rel='manifest' href='/manifest.webmanifest'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font:16px system-ui;max-width:52rem;margin:2rem auto;padding:0 1rem;line-height:1.5}"
            "input,textarea,select,button{font:inherit;padding:.45rem}textarea{width:100%;box-sizing:border-box}"
            "label{display:block;margin:.8rem 0}pre{white-space:pre-wrap;background:#eee;padding:1rem}"
            "li{margin:.5rem 0}a{color:#064c9e}</style>"
            "<header><a href='/'>Teem</a></header>" + content + "</html>").encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "Teem/0.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, format, *args):
        pass

    def respond(self, status, data, content_type="application/json"):
        if content_type == "application/json":
            data = canonical(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def auth(self, worker=False):
        value = self.headers.get("Authorization", "")
        if worker:
            expected = "Bearer " + self.app.worker_token
            if not hmac.compare_digest(value, expected):
                fail(401, "worker authentication required")
            version = self.headers.get("X-Teem-Protocol")
            if version != str(PROTOCOL):
                fail(426, "incompatible worker protocol")
        else:
            expected = "Basic " + base64.b64encode(
                (self.app.username + ":" + self.app.password).encode()
            ).decode()
            if not hmac.compare_digest(value, expected):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Teem"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            if self.command == "POST" and self.headers.get("Origin") != self.app.origin:
                fail(403, "same-origin decision required")
        return True

    def body(self, limit=65536):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            fail(400, "invalid content length")
        if length < 0 or length > limit:
            fail(413, "request body too large")
        return self.rfile.read(length)

    def json_body(self):
        try:
            data = json.loads(self.body())
        except (ValueError, UnicodeDecodeError):
            fail(400, "invalid JSON")
        if not isinstance(data, dict):
            fail(400, "JSON object required")
        return data

    def form(self):
        try:
            return {key: values[0] for key, values in parse_qs(self.body().decode()).items()}
        except UnicodeDecodeError:
            fail(400, "invalid form encoding")

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path.startswith("/worker/"):
                if not self.auth(worker=True):
                    return
                fail(404, "not found")
            else:
                if not self.auth():
                    return
                self.browser_get(path)
        except ApiError as exc:
            self.respond(exc.status, {"error": exc.message})
        except psycopg.errors.InvalidTextRepresentation:
            self.respond(400, {"error": "invalid identifier"})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path.startswith("/worker/"):
                if not self.auth(worker=True):
                    return
                self.worker_post(path)
            else:
                if not self.auth():
                    return
                self.browser_post(path)
        except ApiError as exc:
            self.respond(exc.status, {"error": exc.message})
        except psycopg.errors.InvalidTextRepresentation:
            self.respond(400, {"error": "invalid identifier"})
        except psycopg.errors.UniqueViolation:
            self.respond(409, {"error": "conflicting active run or duplicate decision"})

    def browser_get(self, path):
        if path == "/manifest.webmanifest":
            data = {"name": "Teem", "short_name": "Teem", "start_url": "/",
                    "display": "standalone", "theme_color": "#172337", "background_color": "#ffffff",
                    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}
            self.respond(200, canonical(data).encode(), "application/manifest+json")
            return
        if path == "/icon.svg":
            icon = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'>"
                    "<rect width='192' height='192' rx='28' fill='#172337'/>"
                    "<text x='96' y='132' text-anchor='middle' font-size='112' "
                    "font-family='sans-serif' font-weight='bold' fill='white'>T</text></svg>")
            self.respond(200, icon.encode(), "image/svg+xml")
            return
        with connect(self.app.dsn) as conn:
            expire_leases(conn)
            conn.commit()
            if path == "/":
                projects = conn.execute("SELECT id,name FROM projects ORDER BY name").fetchall()
                runs = conn.execute(
                    "SELECT id,project_id,status,created_at FROM runs ORDER BY created_at DESC LIMIT 30"
                ).fetchall()
                options = "".join(f"<option value='{html.escape(p['id'])}'>{html.escape(p['name'])}</option>" for p in projects)
                items = "".join(
                    f"<li><a href='/runs/{r['id']}'>{html.escape(r['project_id'])}: {r['id']}</a> — {html.escape(r['status'])}</li>"
                    for r in runs
                )
                content = ("<h1>New coding request</h1><form method='post' action='/requests'>"
                           f"<label>Project <select name='project'>{options}</select></label>"
                           "<label>Objective <textarea name='objective' required></textarea></label>"
                           "<label>Acceptance criteria <textarea name='criteria' required></textarea></label>"
                           f"<input type='hidden' name='dedupe_key' value='{new_id()}'>"
                           "<button>Propose</button></form><h2>Runs</h2><ul>" + items + "</ul>")
                conn.commit()
                self.respond(200, page("Teem", content), "text/html; charset=utf-8")
                return
            if path.startswith("/runs/") and path.count("/") == 2:
                run_id = path.split("/")[2]
                run = conn.execute(
                    """SELECT r.*, c.body, c.proposal, q.text FROM runs r
                       JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                       JOIN requests q ON q.id=r.request_id WHERE r.id=%s""", (run_id,)
                ).fetchone()
                if not run:
                    fail(404, "run not found")
                candidate = conn.execute("SELECT * FROM candidates WHERE run_id=%s ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
                ev = conn.execute("SELECT kind,created_at FROM events WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()
                contract = run["body"]
                content = f"<h1>Run {run_id}</h1><p>Status: <strong>{html.escape(run['status'])}</strong></p>"
                content += "<h2>Proposal</h2><pre>" + html.escape(json.dumps(run["proposal"], indent=2)) + "</pre>"
                if run["status"] == "awaiting_approval":
                    content += (f"<form method='post' action='/runs/{run_id}/approve'>"
                                f"<input type='hidden' name='version' value='{run['contract_version']}'>"
                                "<button name='decision' value='approve'>Approve coding and checks</button> "
                                "<button name='decision' value='deny'>Deny</button></form>")
                if run["status"] in ("queued", "coding", "uncertain"):
                    content += f"<form method='post' action='/runs/{run_id}/cancel'><button>Cancel</button></form>"
                if run["status"] in ("uncertain", "failed", "checks_failed"):
                    content += f"<form method='post' action='/runs/{run_id}/retry'><button>Retry with new attempt</button></form>"
                if candidate:
                    content += (f"<h2>Candidate {candidate['head_commit']}</h2>"
                                f"<p>Base {candidate['base_commit']} · contract {candidate['contract_version']} "
                                f"· artifact SHA-256 {candidate['artifact_sha256']}</p>"
                                f"<p><a href='/candidates/{candidate['id']}/bundle'>Download Git bundle</a></p>"
                                "<h3>Objective checks</h3><pre>" + html.escape(json.dumps(candidate["evidence"], indent=2)) + "</pre>")
                content += "<h2>Events</h2><ul>" + "".join(
                    f"<li>{html.escape(e['kind'])} — {e['created_at'].isoformat()}</li>" for e in ev
                ) + "</ul>"
                conn.commit()
                self.respond(200, page("Run", content), "text/html; charset=utf-8")
                return
            if path.startswith("/candidates/") and path.endswith("/bundle"):
                candidate_id = path.split("/")[2]
                artifact = conn.execute(
                    """SELECT a.path,a.sha256 FROM candidates c JOIN artifacts a ON a.attempt_id=c.attempt_id
                       WHERE c.id=%s""", (candidate_id,)
                ).fetchone()
                if not artifact:
                    fail(404, "artifact not found")
                data = (self.app.artifact_dir / artifact["path"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                    fail(500, "artifact integrity failure")
                conn.commit()
                self.respond(200, data, "application/x-git-bundle")
                return
        fail(404, "not found")

    def browser_post(self, path):
        form = self.form()
        with connect(self.app.dsn) as conn:
            expire_leases(conn)
            conn.commit()
            if path == "/requests":
                project = conn.execute("SELECT * FROM projects WHERE id=%s", (form.get("project"),)).fetchone()
                objective = form.get("objective", "").strip()
                criteria = form.get("criteria", "").strip()
                if not project or not objective or not criteria or len(objective) > 8000 or len(criteria) > 8000:
                    fail(400, "project, objective, and acceptance criteria required")
                request_id, run_id = new_id(), new_id()
                key = form.get("dedupe_key") or request_id
                existing = conn.execute("SELECT r.id FROM runs r JOIN requests q ON q.id=r.request_id WHERE q.dedupe_key=%s", (key,)).fetchone()
                if existing:
                    conn.commit()
                    return self.redirect("/runs/" + str(existing["id"]))
                body = {"objective": objective, "acceptance_criteria": criteria, "project_id": project["id"],
                        "base_commit": project["base_commit"], "context_version": 1,
                        "allowed_actions": ["code", "check"], "checks": project["checks"],
                        "check_hash": project["check_hash"], "limits": {"seconds": RUN_SECONDS, "attempts": 2},
                        "delivery_condition": "preserved candidate with passing objective checks; awaiting review"}
                proposal = {"repository": project["name"], "base_commit": project["base_commit"],
                            "allowed_actions": body["allowed_actions"], "check_plan": project["checks"],
                            "limits": body["limits"], "delivery_condition": body["delivery_condition"]}
                inserted = conn.execute("""INSERT INTO requests(id,dedupe_key,text,classification)
                                            VALUES (%s,%s,%s,'proposal') ON CONFLICT (dedupe_key) DO NOTHING
                                            RETURNING id""",
                                        (request_id, key, objective + "\n\nAcceptance criteria:\n" + criteria)).fetchone()
                if not inserted:
                    existing = conn.execute("SELECT r.id FROM runs r JOIN requests q ON q.id=r.request_id WHERE q.dedupe_key=%s",
                                            (key,)).fetchone()
                    if existing:
                        conn.commit()
                        return self.redirect("/runs/" + str(existing["id"]))
                    fail(409, "request is still being created")
                conn.execute("""INSERT INTO runs(id,request_id,project_id,status,deadline)
                                VALUES (%s,%s,%s,'awaiting_approval',now()+(%s || ' seconds')::interval)""",
                             (run_id, request_id, project["id"], RUN_SECONDS))
                conn.execute("INSERT INTO contracts(run_id,version,body,proposal) VALUES (%s,1,%s::jsonb,%s::jsonb)",
                             (run_id, canonical(body), canonical(proposal)))
                event(conn, run_id, "proposal_created", {"contract_version": 1, "request_id": request_id})
                conn.commit()
                return self.redirect("/runs/" + run_id)
            if path.startswith("/runs/"):
                parts = path.split("/")
                if len(parts) != 4:
                    fail(404, "not found")
                run_id, action = parts[2], parts[3]
                run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
                if not run:
                    fail(404, "run not found")
                if action == "approve":
                    if run["status"] != "awaiting_approval" or form.get("version") != str(run["contract_version"]):
                        fail(409, "decision is stale")
                    decision = form.get("decision")
                    if decision not in ("approve", "deny"):
                        fail(400, "specific decision required")
                    conn.execute("""INSERT INTO approvals(id,run_id,contract_version,action,decision,source,target,target_state)
                                    VALUES (%s,%s,%s,'code_and_check',%s,'authenticated_ui',%s,'awaiting_approval')""",
                                 (new_id(), run_id, run["contract_version"], decision, run["project_id"]))
                    event(conn, run_id, "approval_decided", {"action": "code_and_check", "decision": decision})
                    if decision == "approve":
                        conn.execute("""UPDATE runs SET status='queued',updated_at=now(),
                                        deadline=now()+(%s || ' seconds')::interval WHERE id=%s""", (RUN_SECONDS, run_id))
                        conn.execute("INSERT INTO tasks(id,run_id,kind,status) VALUES (%s,%s,'code_and_check','queued')", (new_id(), run_id))
                        event(conn, run_id, "task_queued", {"kind": "code_and_check"})
                    else:
                        conn.execute("UPDATE runs SET status='denied',updated_at=now() WHERE id=%s", (run_id,))
                elif action == "cancel":
                    if run["status"] not in ("queued", "coding", "uncertain"):
                        fail(409, "run cannot be cancelled")
                    pending = run["status"] != "queued"
                    status = "cancelling" if pending else "cancelled"
                    conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (status, run_id))
                    conn.execute("UPDATE tasks SET cancel_requested=true,status=%s WHERE run_id=%s", (status, run_id))
                    event(conn, run_id, "cancel_requested", {})
                elif action == "retry":
                    if run["status"] not in ("uncertain", "failed", "checks_failed"):
                        fail(409, "run cannot be retried")
                    task = conn.execute("SELECT * FROM tasks WHERE run_id=%s FOR UPDATE", (run_id,)).fetchone()
                    latest = conn.execute("SELECT status FROM attempts WHERE task_id=%s ORDER BY generation DESC LIMIT 1", (task["id"],)).fetchone()
                    if latest and latest["status"] in ("running", "uncertain"):
                        fail(409, "worker reconciliation required before retry")
                    if task["generation"] >= 2 or run["deadline"] < datetime.now(timezone.utc):
                        fail(409, "run limit reached")
                    conn.execute("UPDATE attempts SET status='superseded' WHERE task_id=%s AND status IN ('running','uncertain')", (task["id"],))
                    conn.execute("UPDATE tasks SET status='queued',cancel_requested=false WHERE id=%s", (task["id"],))
                    conn.execute("UPDATE runs SET status='queued',updated_at=now() WHERE id=%s", (run_id,))
                    event(conn, run_id, "retry_queued", {"next_generation": task["generation"] + 1})
                else:
                    fail(404, "not found")
                conn.commit()
                return self.redirect("/runs/" + run_id)
        fail(404, "not found")

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def worker_post(self, path):
        if path == "/worker/claim":
            data = self.json_body()
            if data.get("worker_id") != self.app.worker_id or data.get("capabilities") != ["code", "check", "bundle"]:
                fail(403, "worker identity or capabilities mismatch")
            with connect(self.app.dsn) as conn:
                expire_leases(conn)
                conn.commit()
                existing = conn.execute("""SELECT a.id AS attempt_id,a.generation,a.status,t.id AS task_id,
                                          t.run_id,r.project_id,r.contract_version,c.body
                                          FROM attempts a JOIN tasks t ON t.id=a.task_id
                                          JOIN runs r ON r.id=t.run_id
                                          JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                                          WHERE a.worker_id=%s AND a.status='running'
                                          AND a.lease_until>now() AND r.status='coding'
                                          AND NOT t.cancel_requested AND r.deadline>now()
                                          ORDER BY a.started_at LIMIT 1""",
                                        (self.app.worker_id,)).fetchone()
                if existing:
                    conn.commit()
                    self.respond(200, {"assignment": {"attempt_id": str(existing["attempt_id"]),
                        "task_id": str(existing["task_id"]), "generation": existing["generation"],
                        "project_id": existing["project_id"], "contract_version": existing["contract_version"],
                        "contract": existing["body"], "lease_seconds": LEASE_SECONDS}})
                    return
                selected = conn.execute("""SELECT t.id AS task_id FROM tasks t
                                           JOIN runs r ON r.id=t.run_id
                                           JOIN approvals a ON a.run_id=r.id AND a.contract_version=r.contract_version
                                           WHERE t.status='queued' AND r.status='queued' AND a.action='code_and_check'
                                           AND a.decision='approve' AND r.deadline>now()
                                           ORDER BY t.created_at FOR UPDATE OF r SKIP LOCKED LIMIT 1""").fetchone()
                if not selected:
                    conn.commit()
                    self.respond(200, {"assignment": None})
                    return
                task = conn.execute("""SELECT t.*,r.project_id,r.contract_version,r.deadline,c.body
                                       FROM tasks t JOIN runs r ON r.id=t.run_id
                                       JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                                       WHERE t.id=%s FOR UPDATE OF t""", (selected["task_id"],)).fetchone()
                attempt_id = new_id()
                generation = task["generation"] + 1
                conn.execute("UPDATE tasks SET status='running',generation=%s WHERE id=%s", (generation, task["id"]))
                conn.execute("UPDATE runs SET status='coding',updated_at=now() WHERE id=%s", (task["run_id"],))
                conn.execute("""INSERT INTO attempts(id,task_id,generation,worker_id,status,lease_until)
                                VALUES (%s,%s,%s,%s,'running',now()+(%s || ' seconds')::interval)""",
                             (attempt_id, task["id"], generation, self.app.worker_id, LEASE_SECONDS))
                event(conn, task["run_id"], "attempt_assigned", {"attempt_id": attempt_id, "generation": generation})
                conn.commit()
                self.respond(200, {"assignment": {"attempt_id": attempt_id, "task_id": str(task["id"]),
                    "generation": generation, "project_id": task["project_id"], "contract_version": task["contract_version"],
                    "contract": task["body"], "lease_seconds": LEASE_SECONDS}})
                return
        parts = path.split("/")
        if len(parts) != 4 or parts[1] != "worker":
            fail(404, "not found")
        operation, attempt_id = parts[2], parts[3]
        if operation == "reconcile":
            data = self.json_body()
            with connect(self.app.dsn) as conn:
                expire_leases(conn)
                conn.commit()
                row = self.lock_attempt(conn, attempt_id)
                if data.get("generation") != row["generation"]:
                    fail(409, "stale attempt generation")
                if row["generation"] != row["task_generation"]:
                    conn.commit()
                    self.respond(200, {"status": "superseded", "cancel": False})
                    return
                state, outcome = data.get("journal_state"), data.get("outcome")
                if state not in ("accepted", "preparing", "executing", "ready_to_report") or \
                   (state == "ready_to_report" and outcome not in ("candidate", "failed", "uncertain", "cancelled")) or \
                   (state != "ready_to_report" and outcome is not None):
                    fail(400, "invalid worker journal state")
                if row["status"] in ("candidate", "failed", "cancelled", "reconciled_uncertain", "superseded"):
                    conn.commit()
                    self.respond(200, {"status": row["status"], "cancel": row["cancel_requested"]})
                    return
                if row["status"] not in ("running", "uncertain"):
                    fail(409, "attempt cannot be reconciled")
                if row["cancel_requested"]:
                    status, attempt_status = "cancelled", "cancelled"
                elif state != "ready_to_report" or outcome == "uncertain" or row["deadline"] < datetime.now(timezone.utc):
                    status, attempt_status = "uncertain", "reconciled_uncertain"
                else:
                    conn.execute("UPDATE attempts SET status='running',lease_until=now()+(%s || ' seconds')::interval WHERE id=%s",
                                 (LEASE_SECONDS, attempt_id))
                    conn.execute("UPDATE tasks SET status='running' WHERE id=%s", (row["task_id"],))
                    conn.execute("UPDATE runs SET status='coding',updated_at=now() WHERE id=%s", (row["run_id"],))
                    event(conn, row["run_id"], "attempt_reconciled", {"attempt_id": attempt_id, "journal_state": state})
                    conn.commit()
                    self.respond(200, {"status": "running", "cancel": False})
                    return
                conn.execute("UPDATE attempts SET status=%s,finished_at=now() WHERE id=%s", (attempt_status, attempt_id))
                conn.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, row["task_id"]))
                conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (status, row["run_id"]))
                event(conn, row["run_id"], "attempt_reconciled", {"attempt_id": attempt_id, "journal_state": state, "status": status})
                conn.commit()
                self.respond(200, {"status": attempt_status, "cancel": row["cancel_requested"]})
                return
        if operation == "upload":
            data = self.body(MAX_ARTIFACT_BYTES)
            sha = hashlib.sha256(data).hexdigest()
            if sha != self.headers.get("X-Artifact-SHA256"):
                fail(400, "artifact hash mismatch")
            with connect(self.app.dsn) as conn:
                row = self.lock_attempt(conn, attempt_id)
                if row["deadline"] < datetime.now(timezone.utc):
                    fail(409, "run deadline exceeded")
                if row["status"] != "running" or row["lease_until"] < datetime.now(timezone.utc) or \
                   row["generation"] != row["task_generation"] or row["cancel_requested"]:
                    fail(409, "attempt has no live lease")
                prior = conn.execute("SELECT sha256 FROM artifacts WHERE attempt_id=%s", (attempt_id,)).fetchone()
                if prior and prior["sha256"] != sha:
                    fail(409, "artifact identity changed")
                if not prior:
                    with tempfile.NamedTemporaryFile(dir=self.app.artifact_dir, delete=False) as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                        temp_name = f.name
                    os.replace(temp_name, self.app.artifact_dir / (sha + ".bundle"))
                    dir_fd = os.open(self.app.artifact_dir, os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                    conn.execute("INSERT INTO artifacts(attempt_id,sha256,size_bytes,path) VALUES (%s,%s,%s,%s)",
                                 (attempt_id, sha, len(data), sha + ".bundle"))
                    event(conn, row["run_id"], "artifact_preserved", {"attempt_id": attempt_id, "sha256": sha})
            self.respond(200, {"sha256": sha})
            return
        data = self.json_body()
        if data.get("generation") is None:
            fail(400, "generation required")
        with connect(self.app.dsn) as conn:
            row = self.lock_attempt(conn, attempt_id)
            if row["deadline"] < datetime.now(timezone.utc):
                fail(409, "run deadline exceeded")
            if row["generation"] != data["generation"] or row["generation"] != row["task_generation"] or \
               row["status"] != "running" or row["lease_until"] < datetime.now(timezone.utc):
                fail(409, "stale attempt or lease")
            if operation == "renew":
                if row["cancel_requested"]:
                    self.respond(200, {"cancel": True})
                    return
                conn.execute("UPDATE attempts SET lease_until=now()+(%s || ' seconds')::interval WHERE id=%s",
                             (LEASE_SECONDS, attempt_id))
                event(conn, row["run_id"], "lease_renewed", {"attempt_id": attempt_id})
                conn.commit()
                self.respond(200, {"cancel": False, "lease_seconds": LEASE_SECONDS})
                return
            if operation == "report":
                outcome = data.get("outcome")
                if outcome not in ("candidate", "failed", "uncertain", "cancelled"):
                    fail(400, "invalid outcome")
                if row["cancel_requested"] and outcome != "cancelled":
                    fail(409, "run cancelled")
                status = {"candidate": "awaiting_review", "failed": "failed", "uncertain": "uncertain", "cancelled": "cancelled"}[outcome]
                if outcome == "candidate":
                    self.save_candidate(conn, row, data)
                    checks = data["evidence"]["checks"]
                    if any(item["exit_code"] != 0 for item in checks):
                        status = "checks_failed"
                attempt_status = "reconciled_uncertain" if outcome == "uncertain" else outcome
                conn.execute("UPDATE attempts SET status=%s,finished_at=now(),usage=%s::jsonb WHERE id=%s",
                             (attempt_status, canonical(data.get("usage", {})), attempt_id))
                conn.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, row["task_id"]))
                conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (status, row["run_id"]))
                event(conn, row["run_id"], "attempt_reported", {"attempt_id": attempt_id, "outcome": outcome, "run_status": status})
                conn.commit()
                self.respond(200, {"status": status})
                return
        fail(404, "not found")

    def lock_attempt(self, conn, attempt_id):
        row = lock_attempt_rows(conn, attempt_id)
        if not row or row["worker_id"] != self.app.worker_id:
            fail(404, "attempt not found")
        return row

    def save_candidate(self, conn, row, data):
        contract = row["body"]
        evidence = data.get("evidence")
        artifact = conn.execute("SELECT sha256,path FROM artifacts WHERE attempt_id=%s", (row["id"],)).fetchone()
        if not artifact or data.get("artifact_sha256") != artifact["sha256"]:
            fail(409, "candidate artifact not preserved")
        if not (COMMIT_RE.fullmatch(data.get("head_commit", "")) and
                data.get("base_commit") == contract["base_commit"] and
                data.get("contract_version") == row["contract_version"]):
            fail(400, "candidate identity mismatch")
        if not isinstance(evidence, dict) or evidence.get("head_commit") != data["head_commit"] or \
           evidence.get("base_commit") != data["base_commit"] or \
           evidence.get("contract_version") != row["contract_version"] or \
           evidence.get("check_hash") != contract["check_hash"]:
            fail(400, "check evidence identity mismatch")
        checks = evidence.get("checks")
        configured = contract["checks"]
        if not isinstance(checks, list) or len(checks) != len(configured):
            fail(400, "missing check results")
        for result, config in zip(checks, configured):
            if not isinstance(result, dict) or result.get("name") != config["name"] or \
               result.get("argv") != config["argv"] or \
               type(result.get("exit_code")) is not int or \
               not isinstance(result.get("output", ""), str):
                fail(400, "check result mismatch")
        if not (self.app.artifact_dir / artifact["path"]).is_file():
            fail(409, "artifact unavailable")
        bundle_path = self.app.artifact_dir / artifact["path"]
        if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != artifact["sha256"]:
            fail(409, "artifact integrity failure")
        with tempfile.TemporaryDirectory() as checkout:
            clone = subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(bundle_path), checkout],
                                   capture_output=True, timeout=120)
            if clone.returncode:
                fail(400, "invalid Git bundle")
            bundle_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                                         capture_output=True, text=True, timeout=30)
            if bundle_head.returncode or bundle_head.stdout.strip() != data["head_commit"]:
                fail(400, "bundle HEAD differs from candidate")
            for revision in (data["base_commit"], data["head_commit"]):
                if subprocess.run(["git", "cat-file", "-e", revision + "^{commit}"], cwd=checkout,
                                  capture_output=True, timeout=30).returncode:
                    fail(400, "bundle lacks candidate revision")
            if subprocess.run(["git", "merge-base", "--is-ancestor", data["base_commit"], data["head_commit"]],
                              cwd=checkout, capture_output=True, timeout=30).returncode:
                fail(400, "candidate does not descend from base")
        conn.execute("""INSERT INTO candidates(id,run_id,attempt_id,contract_version,base_commit,head_commit,
                        artifact_sha256,check_hash,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                     (new_id(), row["run_id"], row["id"], row["contract_version"], data["base_commit"],
                      data["head_commit"], artifact["sha256"], contract["check_hash"], canonical(evidence)))
        event(conn, row["run_id"], "candidate_preserved", {"attempt_id": str(row["id"]), "head_commit": data["head_commit"]})


class App:
    def __init__(self, args):
        self.dsn = args.dsn
        self.artifact_dir = Path(args.artifacts).resolve()
        self.artifact_dir.mkdir(exist_ok=True, mode=0o700)
        for directory in (self.artifact_dir, self.artifact_dir.parent):
            fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self.username = args.username
        self.password = args.password
        self.worker_id = args.worker_id
        self.worker_token = args.worker_token
        self.origin = args.origin


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--dsn", required=True)
    register = sub.add_parser("register-project")
    register.add_argument("--dsn", required=True)
    register.add_argument("--id", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--base", required=True)
    register.add_argument("--checks", required=True, help="JSON file with objective checks")
    serve = sub.add_parser("serve")
    serve.add_argument("--dsn", required=True)
    serve.add_argument("--artifacts", required=True)
    serve.add_argument("--username", required=True)
    serve.add_argument("--password", required=True)
    serve.add_argument("--worker-id", required=True)
    serve.add_argument("--worker-token", required=True)
    serve.add_argument("--origin", required=True, help="External HTTPS origin, e.g. https://teem.example")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.dsn)
    elif args.command == "register-project":
        if not COMMIT_RE.fullmatch(args.base):
            parser.error("base must be a full SHA-1 commit")
        checks = check_config(json.loads(Path(args.checks).read_text()))
        with connect(args.dsn) as conn:
            conn.execute("INSERT INTO projects(id,name,base_commit,checks,check_hash) VALUES (%s,%s,%s,%s::jsonb,%s)",
                         (args.id, args.name, args.base, canonical(checks), digest(checks)))
    else:
        if not all((args.password, args.worker_token, args.origin.startswith("https://"))):
            parser.error("password, worker token, and HTTPS origin required")
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.app = App(args)
        server.serve_forever()


if __name__ == "__main__":
    main()
