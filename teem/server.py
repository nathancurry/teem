import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg

from .common import (
    ApiError,
    COMMIT_RE,
    LEASE_SECONDS,
    MAX_ARTIFACT_BYTES,
    MAX_OUTPUT_BYTES,
    PROTOCOL,
    STATUS_LABELS,
    canonical,
    digest,
    fail,
    new_id,
)
from .db import connect, event, initialize
from .review import ReviewInputError, build_context, validate_result
from .push import sender_loop, valid_subscription, vapid_public_key
from .speech import MAX_AUDIO, SpeechError, SpeechRunner
from . import decider, github, telegram
from .workflow import cancel_run, create_run, decide_run, reviewer_identity, status_rows


def lock_attempt_rows(conn, attempt_id):
    identity = conn.execute("""SELECT t.run_id,a.task_id FROM attempts a
                               JOIN tasks t ON t.id=a.task_id WHERE a.id=%s""", (attempt_id,)).fetchone()
    if not identity:
        return None
    # Every transition that locks these records takes Run, then Task, then Attempt.
    conn.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (identity["run_id"],))
    conn.execute("SELECT id FROM tasks WHERE id=%s FOR UPDATE", (identity["task_id"],))
    return conn.execute("""SELECT a.*,t.run_id,t.cancel_requested,t.generation AS task_generation,
                          t.kind,t.revision_number,t.input_candidate_id,t.review_context,t.status AS task_status,
                          r.contract_version,r.status AS run_status,r.stop_reason,r.current_candidate_id,
                          r.deadline,c.body FROM attempts a JOIN tasks t ON t.id=a.task_id
                          JOIN runs r ON r.id=t.run_id JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                          WHERE a.id=%s FOR UPDATE OF a""", (attempt_id,)).fetchone()


def expire_leases(conn):
    queued = conn.execute("""SELECT id FROM runs WHERE status IN ('queued','awaiting_review')
                             AND deadline<now() ORDER BY id""").fetchall()
    for candidate in queued:
        run = conn.execute("SELECT status,deadline FROM runs WHERE id=%s FOR UPDATE", (candidate["id"],)).fetchone()
        if run["status"] not in ("queued", "awaiting_review") or run["deadline"] >= datetime.now(timezone.utc):
            continue
        conn.execute("UPDATE runs SET status='blocked',stop_reason='deadline',updated_at=now() WHERE id=%s", (candidate["id"],))
        conn.execute("UPDATE tasks SET status='cancelled' WHERE run_id=%s AND status='queued'", (candidate["id"],))
        event(conn, candidate["id"], "deadline_exceeded", {}, notify=True)
    rows = conn.execute("""SELECT a.id FROM attempts a JOIN tasks t ON t.id=a.task_id
                           JOIN runs r ON r.id=t.run_id WHERE a.status='running'
                           AND (a.lease_until < now() OR (r.deadline < now() AND NOT t.cancel_requested))
                           ORDER BY r.id,t.id,a.id""").fetchall()
    for candidate in rows:
        row = lock_attempt_rows(conn, candidate["id"])
        if not row:
            continue
        now = datetime.now(timezone.utc)
        if row["status"] != "running" or (row["lease_until"] >= now and
            (row["deadline"] >= now or row["cancel_requested"])):
            continue
        conn.execute("UPDATE attempts SET status='uncertain' WHERE id=%s", (row["id"],))
        status = "cancelling" if row["cancel_requested"] else "uncertain"
        conn.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, row["task_id"]))
        conn.execute("UPDATE runs SET status=%s, updated_at=now() WHERE id=%s", (status, row["run_id"]))
        kind = "deadline_exceeded" if row["deadline"] < now else "lease_expired"
        event(conn, row["run_id"], kind, {"attempt_id": str(row["id"])}, notify=status == "uncertain" and row["run_status"] != "uncertain")


def attempt_count(conn, run_id):
    return conn.execute("""SELECT count(*) AS n FROM attempts a JOIN tasks t ON t.id=a.task_id
                           WHERE t.run_id=%s""", (run_id,)).fetchone()["n"]


def stop_run(conn, run_id, reason, kind="blocked"):
    prior = conn.execute("SELECT status FROM runs WHERE id=%s", (run_id,)).fetchone()["status"]
    conn.execute("UPDATE runs SET status=%s,stop_reason=%s,updated_at=now() WHERE id=%s", (kind, reason, run_id))
    event(conn, run_id, "run_stopped", {"status": kind, "reason": reason},
          notify=prior != kind and kind in ("blocked", "failed", "checks_failed", "uncertain"))


def revoke_ready_for_artifact(conn, candidate):
    run = conn.execute("SELECT status,current_candidate_id FROM runs WHERE id=%s FOR UPDATE",
                       (candidate["run_id"],)).fetchone()
    if run["status"] == "ready_to_merge" and run["current_candidate_id"] == candidate["id"]:
        stop_run(conn, candidate["run_id"], "integrity_failure")


def prepare_reviews(app):
    with connect(app.dsn) as conn:
        rows = conn.execute("SELECT id FROM runs WHERE status='awaiting_review' ORDER BY id").fetchall()
    for item in rows:
        run_id = item["id"]
        with connect(app.dsn) as conn:
            row = conn.execute("""SELECT r.*,c.body,p.proposal,x.*,a.path AS artifact_path
                                  FROM runs r JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                                  JOIN candidates x ON x.id=r.current_candidate_id
                                  LEFT JOIN artifacts a ON a.attempt_id=x.attempt_id
                                  JOIN contracts p ON p.run_id=r.id AND p.version=r.contract_version
                                  WHERE r.id=%s AND r.status='awaiting_review'""", (run_id,)).fetchone()
            existing = conn.execute("""SELECT 1 FROM tasks WHERE run_id=%s AND kind='review'
                                       AND input_candidate_id=(SELECT current_candidate_id FROM runs WHERE id=%s)""",
                                    (run_id, run_id)).fetchone()
        if not row or existing:
            continue
        contract = row["body"]
        reviewer = app.reviewer
        try:
            if not reviewer or contract["reviewer"] != reviewer_identity(reviewer):
                raise ReviewInputError("reviewer configuration changed")
            if not row["artifact_path"]:
                raise ReviewInputError("candidate bundle unavailable")
            bundle = app.artifact_dir / row["artifact_path"]
            try:
                bundle_data = bundle.read_bytes()
            except OSError as exc:
                raise ReviewInputError("candidate bundle unavailable") from exc
            if hashlib.sha256(bundle_data).hexdigest() != row["artifact_sha256"]:
                raise ReviewInputError("candidate bundle integrity failure")
            context = build_context(bundle, row, contract, reviewer)
        except (ReviewInputError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            with connect(app.dsn) as conn:
                run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
                if run["status"] == "awaiting_review" and run["current_candidate_id"] == row["id"]:
                    reason = "integrity_failure" if "bundle" in str(exc) else "review_input_unavailable"
                    stop_run(conn, run_id, reason)
            continue
        with connect(app.dsn) as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if run["status"] != "awaiting_review" or run["current_candidate_id"] != row["id"] or \
               run["contract_version"] != row["contract_version"]:
                continue
            if run["deadline"] <= datetime.now(timezone.utc):
                stop_run(conn, run_id, "deadline")
                continue
            coding = conn.execute("""SELECT t.revision_number FROM tasks t JOIN attempts a ON a.task_id=t.id
                                     WHERE a.id=%s FOR UPDATE OF t""", (row["attempt_id"],)).fetchone()
            if conn.execute("SELECT 1 FROM tasks WHERE run_id=%s AND kind='review' AND revision_number=%s",
                            (run_id, coding["revision_number"])).fetchone():
                continue
            if attempt_count(conn, run_id) >= contract["limits"]["attempts"]:
                stop_run(conn, run_id, "attempt_limit")
                continue
            conn.execute("""INSERT INTO tasks(id,run_id,kind,status,revision_number,input_candidate_id,review_context)
                            VALUES (%s,%s,'review','queued',%s,%s,%s::jsonb)
                            ON CONFLICT (run_id,revision_number,kind) DO NOTHING""",
                         (new_id(), run_id, coding["revision_number"], row["id"], canonical(context)))
            event(conn, run_id, "review_queued", {"candidate_id": str(row["id"]), "context_sha256": context["sha256"]})


def assignment(row):
    return {"attempt_id": str(row["attempt_id"]), "task_id": str(row["task_id"]),
            "generation": row["generation"], "project_id": row["project_id"],
            "contract_version": row["contract_version"], "contract": row["body"],
            "kind": row["kind"], "revision_number": row["revision_number"],
            "input_candidate_id": str(row["input_candidate_id"]) if row["input_candidate_id"] else None,
            "source_review_attempt_id": str(row["source_review_attempt_id"]) if row["source_review_attempt_id"] else None,
            "source_review": row.get("source_review"),
            "input_head": row.get("input_head"),
            "input_artifact_sha256": row.get("input_artifact_sha256"),
            "parent_check_evidence": row.get("parent_check_evidence"),
            "review_context": row["review_context"], "lease_seconds": LEASE_SECONDS,
            "deadline": row["deadline"].isoformat()}


def page(title, content):
    return ("<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='theme-color' content='#172337'>"
            "<link rel='manifest' href='/manifest.webmanifest'>"
            "<link rel='apple-touch-icon' href='/icon-192.png'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font:16px system-ui;max-width:52rem;margin:2rem auto;padding:0 1rem;line-height:1.5}"
            "input,textarea,select,button{font:inherit;padding:.45rem}textarea{width:100%;box-sizing:border-box}"
            "label{display:block;margin:.8rem 0}pre{white-space:pre-wrap;background:#eee;padding:1rem}"
            "li{margin:.5rem 0}a{color:#064c9e}</style>"
            "<header><a href='/'>Teem</a></header>" + content +
            "<script src='/phone.js' defer></script></html>").encode()


class ThreadingHTTPServer(HTTPServer):
    daemon_threads = True

    def server_close(self):
        if hasattr(self, "app"):
            self.app.stop()
        super().server_close()


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
            if path in ("/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png",
                        "/phone.js", "/sw.js"):
                self.static_get(path)
                return
            if path.startswith("/worker/"):
                if not self.auth(worker=True):
                    return
                if path.startswith("/worker/bundle/"):
                    candidate_id = path.split("/")[-1]
                    with connect(self.app.dsn) as conn:
                        candidate = conn.execute("""SELECT c.*,a.path FROM candidates c LEFT JOIN artifacts a
                                                   ON a.attempt_id=c.attempt_id WHERE c.id=%s""",
                                                 (candidate_id,)).fetchone()
                        if not candidate:
                            fail(404, "candidate not found")
                        try:
                            data = (self.app.artifact_dir / candidate["path"]).read_bytes() if candidate["path"] else b""
                        except OSError:
                            data = b""
                        if not candidate["path"] or hashlib.sha256(data).hexdigest() != candidate["artifact_sha256"]:
                            revoke_ready_for_artifact(conn, candidate)
                            conn.commit()
                            fail(409, "artifact integrity failure")
                    self.respond(200, data, "application/x-git-bundle")
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

    def static_get(self, path):
        if path == "/manifest.webmanifest":
            data = {"id": "/", "name": "Teem", "short_name": "Teem", "start_url": "/",
                    "scope": "/", "display": "standalone", "theme_color": "#172337",
                    "background_color": "#ffffff",
                    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
            self.respond(200, canonical(data).encode(), "application/manifest+json")
            return
        static = Path(__file__).with_name("static")
        name, content_type = {"/icon.svg": ("icon.svg", "image/svg+xml"),
                              "/icon-192.png": ("icon-192.png", "image/png"),
                              "/icon-512.png": ("icon-512.png", "image/png"),
                              "/phone.js": ("phone.js", "text/javascript; charset=utf-8"),
                              "/sw.js": ("sw.js", "text/javascript; charset=utf-8")}[path]
        self.respond(200, (static / name).read_bytes(), content_type)

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

    def transcribe(self):
        speech = self.app.speech
        if speech is None:
            fail(503, "local recognition unavailable")
        if not speech.lock.acquire(blocking=False):
            fail(429, "transcription busy")
        try:
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                fail(400, "invalid content length")
            if length <= 0 or length > MAX_AUDIO:
                fail(413, "recording too large or empty")
            deadline = time.monotonic() + 30
            parts = []
            try:
                while length:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        fail(408, "upload timed out")
                    self.connection.settimeout(remaining)
                    part = self.rfile.read(min(length, 65536))
                    if not part:
                        fail(400, "incomplete recording")
                    parts.append(part)
                    length -= len(part)
            except (socket.timeout, TimeoutError):
                fail(408, "upload timed out")
            audio = b"".join(parts)
            try:
                text = speech._transcribe(audio, media_type)
            except SpeechError as exc:
                fail(400 if "invalid" in str(exc) or "unsupported" in str(exc) else 504,
                     str(exc))
            self.respond(200, {"text": text})
        finally:
            speech.lock.release()

    def browser_get(self, path):
        if path == "/config":
            self.respond(200, {"vapid_public_key": self.app.vapid_public_key,
                               "speech_available": self.app.speech is not None})
            return
        if path == "/state" or path.startswith("/runs/") and path.endswith("/state"):
            run_id = path.split("/")[2] if path != "/state" else None
            with connect(self.app.dsn) as conn:
                expire_leases(conn)
                rows = status_rows(conn, run_id)
                if run_id and not rows:
                    fail(404, "run not found")
            self.respond(200, {"refreshed_at": datetime.now(timezone.utc).isoformat(), "runs": rows})
            return
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
                runs = status_rows(conn)
                options = "".join(f"<option value='{html.escape(p['id'])}'>{html.escape(p['name'])}</option>" for p in projects)
                items = "".join(
                    f"<li><a href='/runs/{r['id']}'>{html.escape(r['project'])}: {html.escape(r['objective'])}</a>"
                    f" — {html.escape(r['label'])}, round {r['round']}. {html.escape(r['summary'])}</li>"
                    for r in runs
                )
                content = ("<h1>New coding request</h1><form method='post' action='/requests'>"
                           f"<label>Project <select name='project'>{options}</select></label>"
                           "<p>Local recognition runs on the Teem server, not this phone. A recording is uploaded"
                           " to this server for transcription and is discarded afterward.</p>"
                           "<label>Objective <textarea id='objective' name='objective' required></textarea></label>"
                           "<button type='button' class='mic' data-field='objective' aria-label='Record objective'>🎙 Record objective</button>"
                           "<label>Acceptance criteria <textarea id='criteria' name='criteria' required></textarea></label>"
                           "<button type='button' class='mic' data-field='criteria' aria-label='Record acceptance criteria'>🎙 Record criteria</button>"
                           "<p id='recording-status' role='status' aria-live='polite'></p>"
                           "<label>Revision limit <select name='revisions'><option>0</option><option>1</option>"
                           "<option selected>2</option></select></label>"
                           f"<input type='hidden' name='dedupe_key' value='{new_id()}'>"
                           "<button>Propose</button></form>"
                           "<section id='notifications'><button type='button' id='enable-push'>Enable notifications</button> "
                           "<button type='button' id='disable-push'>Disable notifications</button> "
                           "<span id='push-status' role='status'></span></section>"
                           "<p id='refresh-status' role='status'></p><h2>Pending approvals and Runs</h2>"
                           "<ul id='run-list'>" + items + "</ul>")
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
                candidates = conn.execute("SELECT * FROM candidates WHERE run_id=%s ORDER BY created_at,id", (run_id,)).fetchall()
                reviews = conn.execute("""SELECT v.*,t.input_candidate_id,t.revision_number FROM reviews v
                                          JOIN attempts a ON a.id=v.attempt_id JOIN tasks t ON t.id=a.task_id
                                          WHERE t.run_id=%s ORDER BY v.created_at,v.attempt_id""", (run_id,)).fetchall()
                tasks = conn.execute("SELECT kind,revision_number,status,review_context FROM tasks WHERE run_id=%s ORDER BY revision_number,kind", (run_id,)).fetchall()
                ev = conn.execute("SELECT kind,created_at FROM events WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()
                contract = run["body"]
                visible_status = STATUS_LABELS.get(run["status"], run["status"])
                content = (f"<h1>Run {run_id}</h1><p>{html.escape(contract['project_id'])}: "
                           f"{html.escape(contract['objective'])}</p><p>Status: <strong id='run-status' "
                           f"data-status='{html.escape(run['status'])}'>{html.escape(visible_status)}</strong></p>")
                content += "<h2>Proposal</h2><pre>" + html.escape(json.dumps(run["proposal"], indent=2)) + "</pre>"
                if run["status"] == "awaiting_approval":
                    content += "<h2>Exact scope for this decision</h2><pre>" + html.escape(json.dumps(contract, indent=2)) + "</pre>"
                content += f"<p>Stop reason: {html.escape(run['stop_reason'] or 'none')}</p>"
                used = attempt_count(conn, run_id)
                rounds = max((t["revision_number"] for t in tasks if t["kind"] == "code_and_check"), default=0)
                seconds_left = max(0, int((run["deadline"] - datetime.now(timezone.utc)).total_seconds()))
                content += (f"<p>Round: {rounds} · Attempts remaining: {max(0,contract['limits']['attempts']-used)} · "
                            f"revisions remaining: {max(0,contract['limits']['revisions']-rounds)} · "
                            f"time remaining: {seconds_left} seconds · deadline: {run['deadline'].isoformat()}</p>")
                if run["status"] == "awaiting_approval":
                    content += (f"<form method='post' action='/runs/{run_id}/approve'>"
                                f"<input type='hidden' name='version' value='{run['contract_version']}'>"
                                "<button name='decision' value='approve'>Approve coding, checks, review, and bounded revisions</button> "
                                "<button name='decision' value='deny'>Deny</button></form>")
                if run["status"] in ("queued", "coding", "awaiting_review", "reviewing", "uncertain"):
                    content += f"<form method='post' action='/runs/{run_id}/cancel'><button>Cancel</button></form>"
                if run["status"] in ("uncertain", "failed"):
                    content += f"<form method='post' action='/runs/{run_id}/retry'><button>Retry with new attempt</button></form>"
                for candidate in candidates:
                    label = "current" if candidate["id"] == run["current_candidate_id"] else "historical"
                    content += (f"<h2>Candidate {candidate['head_commit']} ({label})</h2>"
                                f"<p>Base {candidate['base_commit']} · contract {candidate['contract_version']} "
                                f"· artifact SHA-256 {candidate['artifact_sha256']}</p>"
                                f"<p><a href='/candidates/{candidate['id']}/bundle'>Download Git bundle</a></p>"
                                "<h3>Objective checks</h3><pre>" + html.escape(json.dumps(candidate["evidence"], indent=2)) + "</pre>")
                for review in reviews:
                    label = review["disposition"] if review["disposition"] != "accepted" else (
                        "current" if review["input_candidate_id"] == run["current_candidate_id"] else "historical")
                    content += (f"<h2>Review round {review['revision_number']} ({label})</h2>"
                                "<pre>" + html.escape(json.dumps(review["result"] or {"error": review["validation_error"],
                                                                           "raw_output": review["raw_output"]}, indent=2)) + "</pre>")
                for task in tasks:
                    if task["kind"] == "review" and task["review_context"]:
                        content += (f"<p><a href='/tasks/{task['revision_number']}/{run_id}/context'>"
                                    f"Review context round {task['revision_number']}</a></p>")
                content += "<h2>Events</h2><ul>" + "".join(
                    f"<li>{html.escape(e['kind'])} — {e['created_at'].isoformat()}</li>" for e in ev
                ) + "</ul><p id='refresh-status' role='status'></p>"
                conn.commit()
                self.respond(200, page("Run", content), "text/html; charset=utf-8")
                return
            if path.startswith("/candidates/") and path.endswith("/bundle"):
                candidate_id = path.split("/")[2]
                artifact = conn.execute(
                    """SELECT a.path,a.sha256,c.run_id,c.id,c.artifact_sha256 FROM candidates c
                       LEFT JOIN artifacts a ON a.attempt_id=c.attempt_id
                       WHERE c.id=%s""", (candidate_id,)
                ).fetchone()
                if not artifact:
                    fail(404, "artifact not found")
                try:
                    data = (self.app.artifact_dir / artifact["path"]).read_bytes() if artifact["path"] else b""
                except OSError:
                    data = b""
                if hashlib.sha256(data).hexdigest() != artifact["artifact_sha256"] or \
                   artifact["sha256"] != artifact["artifact_sha256"]:
                    revoke_ready_for_artifact(conn, artifact)
                    conn.commit()
                    fail(500, "artifact integrity failure")
                conn.commit()
                self.respond(200, data, "application/x-git-bundle")
                return
            if path.startswith("/tasks/") and path.endswith("/context"):
                parts = path.split("/")
                if len(parts) != 5:
                    fail(404, "not found")
                task = conn.execute("""SELECT review_context FROM tasks WHERE run_id=%s AND revision_number=%s
                                       AND kind='review'""", (parts[3], parts[2])).fetchone()
                if not task:
                    fail(404, "context not found")
                self.respond(200, task["review_context"])
                return
        fail(404, "not found")

    def browser_post(self, path):
        if path == "/transcribe":
            return self.transcribe()
        if path == "/push/subscribe":
            if not self.app.vapid_public_key:
                fail(503, "notifications unavailable")
            data = self.json_body()
            if not valid_subscription(data):
                fail(400, "invalid push subscription")
            previous = data.get("previous_id")
            # A replaced install may remove its old endpoint after registering a new one.
            with connect(self.app.dsn) as conn:
                row = conn.execute("""INSERT INTO push_subscriptions(id,endpoint,p256dh,auth)
                                      VALUES (%s,%s,%s,%s)
                                      ON CONFLICT (endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh,auth=EXCLUDED.auth
                                      RETURNING id""",
                                   (new_id(), data["endpoint"], data["keys"]["p256dh"],
                                    data["keys"]["auth"])).fetchone()
                if previous:
                    try:
                        prior_id = uuid.UUID(previous)
                    except (ValueError, TypeError):
                        fail(400, "invalid previous subscription")
                    if prior_id != row["id"]:
                        conn.execute("DELETE FROM push_subscriptions WHERE id=%s", (prior_id,))
            self.respond(200, {"id": str(row["id"])})
            return
        if path == "/push/unsubscribe":
            data = self.json_body()
            try:
                subscription_id = uuid.UUID(data.get("id", ""))
            except (ValueError, TypeError):
                fail(400, "invalid subscription")
            with connect(self.app.dsn) as conn:
                conn.execute("DELETE FROM push_subscriptions WHERE id=%s", (subscription_id,))
            self.respond(200, {"disabled": True})
            return
        form = self.form()
        with connect(self.app.dsn) as conn:
            expire_leases(conn)
            conn.commit()
            if path == "/requests":
                project = conn.execute("SELECT id FROM projects WHERE id=%s", (form.get("project"),)).fetchone()
                if not project:
                    fail(400, "project, objective, and acceptance criteria required")
                try:
                    revisions = int(form.get("revisions", "2"))
                except ValueError:
                    fail(400, "invalid revision limit")
                try:
                    base, checks = github.fetch_base(self.app, project["id"])
                except github.GitHubError as exc:
                    fail(502, str(exc))
                run_id, _ = create_run(conn, project["id"], base, checks, self.app.reviewer,
                                       form.get("objective", "").strip(), form.get("criteria", "").strip(),
                                       form.get("dedupe_key") or new_id(), revisions)
                conn.commit()
                return self.redirect("/runs/" + run_id)
            if path.startswith("/runs/"):
                parts = path.split("/")
                if len(parts) != 4:
                    fail(404, "not found")
                run_id, action = parts[2], parts[3]
                if action == "approve":
                    decide_run(conn, run_id, form.get("version"), form.get("decision"), "authenticated_ui")
                    conn.commit()
                    return self.redirect("/runs/" + run_id)
                if action == "cancel":
                    cancel_run(conn, run_id)
                    conn.commit()
                    return self.redirect("/runs/" + run_id)
                run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
                if not run:
                    fail(404, "run not found")
                if action == "retry":
                    if run["status"] not in ("uncertain", "failed"):
                        fail(409, "run cannot be retried")
                    task = conn.execute("""SELECT * FROM tasks WHERE run_id=%s AND status IN ('uncertain','failed')
                                           ORDER BY revision_number DESC FOR UPDATE""", (run_id,)).fetchone()
                    if not task:
                        fail(409, "no retryable task")
                    latest = conn.execute("SELECT status FROM attempts WHERE task_id=%s ORDER BY generation DESC LIMIT 1", (task["id"],)).fetchone()
                    if latest and latest["status"] in ("running", "uncertain"):
                        fail(409, "worker reconciliation required before retry")
                    if task["generation"] >= 2 or run["deadline"] < datetime.now(timezone.utc) or \
                       attempt_count(conn, run_id) >= 8:
                        fail(409, "run limit reached")
                    conn.execute("UPDATE attempts SET status='superseded' WHERE task_id=%s AND status IN ('running','uncertain')", (task["id"],))
                    conn.execute("UPDATE tasks SET status='queued',cancel_requested=false WHERE id=%s", (task["id"],))
                    phase = "awaiting_review" if task["kind"] == "review" else "queued"
                    conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (phase, run_id))
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
            capabilities = data.get("capabilities")
            if data.get("worker_id") != self.app.worker_id or capabilities != ["code", "check", "bundle", "review"]:
                fail(403, "worker identity or capabilities mismatch")
            prepare_reviews(self.app)
            with connect(self.app.dsn) as conn:
                expire_leases(conn)
                conn.commit()
                existing = conn.execute("""SELECT a.id AS attempt_id,a.generation,a.status,t.id AS task_id,
                                          t.run_id,t.kind,t.revision_number,t.input_candidate_id,
                                          t.source_review_attempt_id,t.review_context,
                                          r.project_id,r.contract_version,r.deadline,c.body,v.result AS source_review,
                                          parent.head_commit AS input_head,parent.artifact_sha256 AS input_artifact_sha256,
                                          parent.evidence AS parent_check_evidence
                                          FROM attempts a JOIN tasks t ON t.id=a.task_id
                                          JOIN runs r ON r.id=t.run_id
                                          JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                                          LEFT JOIN reviews v ON v.attempt_id=t.source_review_attempt_id
                                          LEFT JOIN candidates parent ON parent.id=t.input_candidate_id
                                          WHERE a.worker_id=%s AND a.status='running'
                                          AND a.lease_until>now() AND r.status IN ('coding','reviewing')
                                          AND NOT t.cancel_requested AND r.deadline>now()
                                          ORDER BY a.started_at LIMIT 1""",
                                        (self.app.worker_id,)).fetchone()
                if existing:
                    conn.commit()
                    self.respond(200, {"assignment": assignment(existing)})
                    return
                selected = conn.execute("""SELECT t.id AS task_id FROM tasks t
                                           JOIN runs r ON r.id=t.run_id
                                           JOIN approvals a ON a.run_id=r.id AND a.contract_version=r.contract_version
                                           WHERE t.status='queued' AND r.status IN ('queued','awaiting_review')
                                           AND ((t.kind='review' AND r.status='awaiting_review') OR
                                                (t.kind='code_and_check' AND r.status='queued'))
                                           AND a.action='code_check_review'
                                           AND a.decision='approve' AND r.deadline>now()
                                           ORDER BY t.created_at FOR UPDATE OF r SKIP LOCKED LIMIT 1""").fetchone()
                if not selected:
                    conn.commit()
                    self.respond(200, {"assignment": None})
                    return
                task = conn.execute("""SELECT t.*,r.project_id,r.contract_version,r.deadline,c.body,
                                       v.result AS source_review,parent.head_commit AS input_head,
                                       parent.artifact_sha256 AS input_artifact_sha256,
                                       parent.evidence AS parent_check_evidence,
                                       r.current_candidate_id FROM tasks t JOIN runs r ON r.id=t.run_id
                                       JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                                       LEFT JOIN reviews v ON v.attempt_id=t.source_review_attempt_id
                                       LEFT JOIN candidates parent ON parent.id=t.input_candidate_id
                                       WHERE t.id=%s FOR UPDATE OF t""", (selected["task_id"],)).fetchone()
                if attempt_count(conn, task["run_id"]) >= task["body"]["limits"]["attempts"]:
                    conn.execute("UPDATE tasks SET status='cancelled' WHERE id=%s", (task["id"],))
                    stop_run(conn, task["run_id"], "attempt_limit")
                    conn.commit()
                    self.respond(200, {"assignment": None})
                    return
                if task["generation"] >= 2:
                    conn.execute("UPDATE tasks SET status='failed' WHERE id=%s", (task["id"],))
                    stop_run(conn, task["run_id"], "attempt_limit")
                    conn.commit()
                    self.respond(200, {"assignment": None})
                    return
                if task["kind"] == "code_and_check" and task["revision_number"] > 0:
                    parent = conn.execute("""SELECT c.*,t.kind AS producing_kind,
                                             t.revision_number AS producing_round FROM candidates c
                                             JOIN attempts a ON a.id=c.attempt_id JOIN tasks t ON t.id=a.task_id
                                             WHERE c.id=%s""", (task["input_candidate_id"],)).fetchone()
                    source = conn.execute("""SELECT v.result,v.disposition,t.input_candidate_id,t.revision_number
                                             FROM reviews v JOIN attempts a ON a.id=v.attempt_id
                                             JOIN tasks t ON t.id=a.task_id WHERE v.attempt_id=%s""",
                                          (task["source_review_attempt_id"],)).fetchone()
                    # A revision answers either an accepted changes_required review or the parent's failed checks.
                    if task["source_review_attempt_id"]:
                        cause_valid = bool(source and source["disposition"] == "accepted" and
                                           parent and source["input_candidate_id"] == parent["id"] and
                                           source["revision_number"] == task["revision_number"] - 1 and
                                           source["result"]["verdict"] == "changes_required")
                    else:
                        cause_valid = bool(parent and any(check["exit_code"] != 0 for check in parent["evidence"]["checks"]))
                    if not parent or parent["run_id"] != task["run_id"] or parent["contract_version"] != task["contract_version"] or \
                       parent["producing_kind"] != "code_and_check" or \
                       parent["producing_round"] != task["revision_number"] - 1 or \
                       parent["id"] != task["current_candidate_id"] or not cause_valid:
                        conn.execute("UPDATE tasks SET status='failed' WHERE id=%s", (task["id"],))
                        stop_run(conn, task["run_id"], "integrity_failure")
                        conn.commit()
                        self.respond(200, {"assignment": None})
                        return
                if task["kind"] == "review":
                    parent = conn.execute("""SELECT c.*,t.revision_number AS producing_round FROM candidates c
                                             JOIN attempts a ON a.id=c.attempt_id JOIN tasks t ON t.id=a.task_id
                                             WHERE c.id=%s""", (task["input_candidate_id"],)).fetchone()
                    context = task["review_context"]
                    if not parent or parent["run_id"] != task["run_id"] or \
                       parent["contract_version"] != task["contract_version"] or \
                       parent["producing_round"] != task["revision_number"] or \
                       parent["id"] != task["current_candidate_id"] or not context or \
                       context["sha256"] != digest(context["pack"]) or \
                       context["pack"]["candidate_id"] != str(parent["id"]) or \
                       context["pack"]["bundle_sha256"] != parent["artifact_sha256"]:
                        conn.execute("UPDATE tasks SET status='failed' WHERE id=%s", (task["id"],))
                        stop_run(conn, task["run_id"], "integrity_failure")
                        conn.commit()
                        self.respond(200, {"assignment": None})
                        return
                attempt_id = new_id()
                generation = task["generation"] + 1
                conn.execute("UPDATE tasks SET status='running',generation=%s WHERE id=%s", (generation, task["id"]))
                phase = "reviewing" if task["kind"] == "review" else "coding"
                conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (phase, task["run_id"]))
                conn.execute("""INSERT INTO attempts(id,task_id,generation,worker_id,status,lease_until)
                                VALUES (%s,%s,%s,%s,'running',now()+(%s || ' seconds')::interval)""",
                             (attempt_id, task["id"], generation, self.app.worker_id, LEASE_SECONDS))
                event(conn, task["run_id"], "attempt_assigned", {"attempt_id": attempt_id, "generation": generation})
                conn.commit()
                self.respond(200, {"assignment": assignment({**task, "task_id": task["id"],
                                                               "attempt_id": attempt_id, "generation": generation})})
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
                state, outcome = data.get("journal_state"), data.get("outcome")
                if state not in ("accepted", "preparing", "executing", "ready_to_report") or \
                   (state == "ready_to_report" and outcome not in ("candidate", "review", "failed", "uncertain", "cancelled")) or \
                   (state != "ready_to_report" and outcome is not None):
                    fail(400, "invalid worker journal state")
                completed_evidence = (state == "ready_to_report" and not row["result_sha256"] and
                                      (outcome == "candidate" and row["kind"] == "code_and_check" or
                                       outcome == "review" and row["kind"] == "review"))
                if row["generation"] != row["task_generation"]:
                    conn.commit()
                    self.respond(200, {"status": "stale_evidence_pending" if completed_evidence else "superseded",
                                       "cancel": False})
                    return
                late_evidence = (state == "ready_to_report" and
                                 (outcome == "candidate" and row["kind"] == "code_and_check" or
                                  outcome == "review" and row["kind"] == "review") and
                                 row["deadline"] < datetime.now(timezone.utc) and not row["cancel_requested"])
                if row["status"] == "reconciled_uncertain" and late_evidence and \
                   row["run_status"] == "blocked" and not row["result_sha256"]:
                    conn.commit()
                    self.respond(200, {"status": "late_evidence_pending", "cancel": False})
                    return
                if completed_evidence and row["status"] in ("cancelled", "reconciled_uncertain", "superseded"):
                    conn.commit()
                    self.respond(200, {"status": "stale_evidence_pending", "cancel": row["cancel_requested"]})
                    return
                if row["status"] in ("candidate", "review", "failed", "cancelled", "reconciled_uncertain", "superseded"):
                    conn.commit()
                    self.respond(200, {"status": row["status"], "cancel": row["cancel_requested"]})
                    return
                if row["status"] not in ("running", "uncertain"):
                    fail(409, "attempt cannot be reconciled")
                if state == "ready_to_report" and outcome in ("candidate", "review") and row["cancel_requested"]:
                    conn.execute("UPDATE attempts SET status='running',lease_until=now()+(%s || ' seconds')::interval WHERE id=%s",
                                 (LEASE_SECONDS, attempt_id))
                    event(conn, row["run_id"], "attempt_reconciled", {"attempt_id": attempt_id, "journal_state": state})
                    conn.commit()
                    self.respond(200, {"status": "running", "cancel": True})
                    return
                if late_evidence:
                    conn.execute("UPDATE attempts SET status='reconciled_uncertain',finished_at=now() WHERE id=%s", (attempt_id,))
                    conn.execute("UPDATE tasks SET status='failed' WHERE id=%s", (row["task_id"],))
                    if row["run_status"] != "blocked":
                        stop_run(conn, row["run_id"], "deadline")
                    event(conn, row["run_id"], "attempt_reconciled", {"attempt_id": attempt_id, "journal_state": state,
                                                                        "status": "late_evidence_pending"})
                    conn.commit()
                    self.respond(200, {"status": "late_evidence_pending", "cancel": False})
                    return
                if row["cancel_requested"]:
                    status, attempt_status = "cancelled", "cancelled"
                elif state != "ready_to_report" or outcome == "uncertain" or row["deadline"] < datetime.now(timezone.utc):
                    status, attempt_status = "uncertain", "reconciled_uncertain"
                else:
                    conn.execute("UPDATE attempts SET status='running',lease_until=now()+(%s || ' seconds')::interval WHERE id=%s",
                                 (LEASE_SECONDS, attempt_id))
                    conn.execute("UPDATE tasks SET status='running' WHERE id=%s", (row["task_id"],))
                    phase = "reviewing" if row["kind"] == "review" else "coding"
                    conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (phase, row["run_id"]))
                    event(conn, row["run_id"], "attempt_reconciled", {"attempt_id": attempt_id, "journal_state": state})
                    conn.commit()
                    self.respond(200, {"status": "running", "cancel": False})
                    return
                conn.execute("UPDATE attempts SET status=%s,finished_at=now() WHERE id=%s", (attempt_status, attempt_id))
                conn.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, row["task_id"]))
                if row["deadline"] < datetime.now(timezone.utc) and not row["cancel_requested"]:
                    status = "blocked"
                    conn.execute("UPDATE runs SET status='blocked',stop_reason='deadline',updated_at=now() WHERE id=%s", (row["run_id"],))
                else:
                    conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (status, row["run_id"]))
                event(conn, row["run_id"], "attempt_reconciled", {"attempt_id": attempt_id, "journal_state": state, "status": status},
                      notify=status in ("blocked", "uncertain") and row["run_status"] != status)
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
                if row["kind"] != "code_and_check":
                    fail(403, "review attempts cannot upload candidates")
                stale_evidence = (not row["result_sha256"] and
                                  (row["generation"] != row["task_generation"] or
                                   row["status"] in ("uncertain", "reconciled_uncertain", "superseded", "cancelled")))
                if not stale_evidence and row["deadline"] < datetime.now(timezone.utc) and not row["cancel_requested"]:
                    fail(409, "run deadline exceeded")
                if not stale_evidence and (row["status"] != "running" or row["lease_until"] < datetime.now(timezone.utc)):
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
            if operation == "report" and row["result_sha256"]:
                received = digest(data)
                if received == row["result_sha256"]:
                    conn.commit()
                    self.respond(200, {"status": row["status"], "replay": True})
                    return
                event(conn, row["run_id"], "conflicting_report", {"attempt_id": attempt_id,
                     "first_sha256": row["result_sha256"], "conflicting_sha256": received})
                affects_current = (
                    row["kind"] == "review" and row["input_candidate_id"] == row["current_candidate_id"] or
                    row["kind"] == "code_and_check" and conn.execute(
                        "SELECT 1 FROM candidates WHERE id=%s AND attempt_id=%s",
                        (row["current_candidate_id"], row["id"])).fetchone())
                if affects_current and row["run_status"] in ("awaiting_review", "ready_to_merge", "queued"):
                    stop_run(conn, row["run_id"], "integrity_failure")
                conn.commit()
                fail(409, "conflicting final report")
            if operation == "report" and row["status"] == "reconciled_uncertain" and \
               row["run_status"] == "blocked" and row["deadline"] < datetime.now(timezone.utc) and \
               data.get("generation") == row["generation"] == row["task_generation"] and \
               (data.get("outcome") == "candidate" and row["kind"] == "code_and_check" or
                data.get("outcome") == "review" and row["kind"] == "review"):
                if row["kind"] == "code_and_check":
                    self.save_candidate(conn, row, data)
                else:
                    self.save_review(conn, row, data, "stale", "run deadline exceeded")
                conn.execute("UPDATE attempts SET status=%s,result_sha256=%s WHERE id=%s",
                             (data["outcome"], digest(data), attempt_id))
                event(conn, row["run_id"], "stale_report", {"attempt_id": attempt_id, "sha256": digest(data)})
                conn.commit()
                self.respond(200, {"status": "stale"})
                return
            stale_evidence = (operation == "report" and data.get("generation") == row["generation"] and
                              (data.get("outcome") == "candidate" and row["kind"] == "code_and_check" or
                               data.get("outcome") == "review" and row["kind"] == "review") and
                              (row["generation"] != row["task_generation"] or
                               row["status"] in ("uncertain", "reconciled_uncertain", "superseded", "cancelled")))
            if stale_evidence:
                if row["kind"] == "code_and_check":
                    self.save_candidate(conn, row, data)
                else:
                    self.save_review(conn, row, data, "stale", "obsolete attempt")
                conn.execute("UPDATE attempts SET status=%s,finished_at=COALESCE(finished_at,now()),result_sha256=%s WHERE id=%s",
                             (data["outcome"], digest(data), attempt_id))
                event(conn, row["run_id"], "stale_report", {"attempt_id": attempt_id, "sha256": digest(data)})
                conn.commit()
                self.respond(200, {"status": "stale"})
                return
            if row["deadline"] < datetime.now(timezone.utc) and not (operation == "report" and row["cancel_requested"]):
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
                if outcome not in ("candidate", "review", "failed", "uncertain", "cancelled"):
                    fail(400, "invalid outcome")
                if row["cancel_requested"] and outcome != "cancelled":
                    if outcome == "review":
                        self.save_review(conn, row, data, "stale", "run cancelled")
                    elif outcome == "candidate":
                        self.save_candidate(conn, row, data)
                    conn.execute("UPDATE attempts SET status=%s,finished_at=now(),result_sha256=%s WHERE id=%s",
                                 (outcome, digest(data), attempt_id))
                    conn.execute("UPDATE tasks SET status='cancelled' WHERE id=%s", (row["task_id"],))
                    conn.execute("UPDATE runs SET status='cancelled',updated_at=now() WHERE id=%s", (row["run_id"],))
                    event(conn, row["run_id"], "late_evidence_preserved", {"attempt_id": attempt_id, "outcome": outcome})
                    conn.commit()
                    self.respond(200, {"status": "cancelled"})
                    return
                if outcome == "candidate" and row["kind"] != "code_and_check" or \
                   outcome == "review" and row["kind"] != "review":
                    fail(400, "outcome does not match task kind")
                status = {"candidate": "awaiting_review", "review": "reviewing", "failed": "failed",
                          "uncertain": "uncertain", "cancelled": "cancelled"}[outcome]
                if outcome == "failed" and data.get("failure_kind") == "policy":
                    status = "blocked"
                    conn.execute("UPDATE runs SET stop_reason='integrity_failure' WHERE id=%s", (row["run_id"],))
                elif outcome == "failed" and data.get("failure_kind") == "needs_input" and row["kind"] == "code_and_check":
                    # The implementer made no change and explained why; that is a question for the user, not a retry.
                    status = "blocked"
                    question = str((data.get("usage") or {}).get("error", ""))[:500]
                    conn.execute("UPDATE runs SET stop_reason=%s WHERE id=%s", ("needs_input: " + question, row["run_id"]))
                if outcome == "candidate":
                    candidate_id = self.save_candidate(conn, row, data)
                    checks = data["evidence"]["checks"]
                    conn.execute("UPDATE runs SET current_candidate_id=%s WHERE id=%s", (candidate_id, row["run_id"]))
                    contract = row["body"]
                    if not any(item["exit_code"] != 0 for item in checks):
                        pass
                    elif row["revision_number"] < contract["limits"]["revisions"] and \
                            attempt_count(conn, row["run_id"]) < contract["limits"]["attempts"]:
                        conn.execute("""INSERT INTO tasks(id,run_id,kind,status,revision_number,input_candidate_id)
                                        VALUES (%s,%s,'code_and_check','queued',%s,%s)""",
                                     (new_id(), row["run_id"], row["revision_number"] + 1, candidate_id))
                        status = "queued"
                        event(conn, row["run_id"], "revision_queued", {"parent_candidate_id": candidate_id,
                                                                        "round": row["revision_number"] + 1,
                                                                        "reason": "checks_failed"})
                    else:
                        status = "checks_failed"
                elif outcome == "review":
                    review, error = self.save_review(conn, row, data, "accepted")
                    conflict = conn.execute("""SELECT 1 FROM events e JOIN candidates c ON c.id=%s
                                               WHERE e.run_id=%s AND e.kind='conflicting_report'
                                               AND e.payload->>'attempt_id' IN (c.attempt_id::text,%s) LIMIT 1""",
                                            (row["input_candidate_id"], row["run_id"], str(row["id"]))).fetchone()
                    if conflict:
                        status = "blocked"
                        conn.execute("UPDATE runs SET stop_reason='integrity_failure' WHERE id=%s", (row["run_id"],))
                    elif error:
                        status = "failed"
                    elif review["verdict"] == "pass":
                        try:
                            self.validate_readiness(conn, row)
                            status = "ready_to_merge"
                        except ReviewInputError:
                            status = "blocked"
                            conn.execute("UPDATE runs SET stop_reason='integrity_failure' WHERE id=%s", (row["run_id"],))
                    elif review["verdict"] == "blocked":
                        status = "blocked"
                        conn.execute("UPDATE runs SET stop_reason='review_uncertain' WHERE id=%s", (row["run_id"],))
                    else:
                        contract = row["body"]
                        if row["revision_number"] >= contract["limits"]["revisions"]:
                            status = "blocked"
                            conn.execute("UPDATE runs SET stop_reason='revision_limit' WHERE id=%s", (row["run_id"],))
                        elif attempt_count(conn, row["run_id"]) >= contract["limits"]["attempts"]:
                            status = "blocked"
                            conn.execute("UPDATE runs SET stop_reason='attempt_limit' WHERE id=%s", (row["run_id"],))
                        else:
                            conn.execute("""INSERT INTO tasks(id,run_id,kind,status,revision_number,input_candidate_id,
                                            source_review_attempt_id) VALUES (%s,%s,'code_and_check','queued',%s,%s,%s)""",
                                         (new_id(), row["run_id"], row["revision_number"] + 1,
                                          row["input_candidate_id"], row["id"]))
                            status = "queued"
                            event(conn, row["run_id"], "revision_queued", {"parent_candidate_id": str(row["input_candidate_id"]),
                                                                            "round": row["revision_number"] + 1})
                attempt_status = ("reconciled_uncertain" if outcome == "uncertain" else
                                  "failed" if outcome == "review" and error else outcome)
                if status == "failed" and outcome != "candidate":
                    if row["generation"] < 2 and attempt_count(conn, row["run_id"]) < row["body"]["limits"]["attempts"]:
                        status = "awaiting_review" if row["kind"] == "review" else "queued"
                    elif attempt_count(conn, row["run_id"]) >= row["body"]["limits"]["attempts"]:
                        status = "blocked"
                        conn.execute("UPDATE runs SET stop_reason='attempt_limit' WHERE id=%s", (row["run_id"],))
                conn.execute("UPDATE attempts SET status=%s,finished_at=now(),usage=%s::jsonb,result_sha256=%s WHERE id=%s",
                             (attempt_status, canonical(data.get("usage", {})), digest(data), attempt_id))
                task_status = ("succeeded" if outcome == "candidate" or outcome == "review" and not error else
                               "queued" if status in ("queued", "awaiting_review") else
                               "cancelled" if status == "cancelled" else "uncertain" if status == "uncertain" else "failed")
                conn.execute("UPDATE tasks SET status=%s WHERE id=%s", (task_status, row["task_id"]))
                conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (status, row["run_id"]))
                event(conn, row["run_id"], "attempt_reported", {"attempt_id": attempt_id, "outcome": outcome, "run_status": status},
                      notify=status in ("ready_to_merge", "blocked", "failed", "checks_failed", "uncertain")
                      and row["run_status"] != status)
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
            if row["input_candidate_id"]:
                parent = conn.execute("SELECT * FROM candidates WHERE id=%s", (row["input_candidate_id"],)).fetchone()
                if not parent or parent["run_id"] != row["run_id"] or parent["contract_version"] != row["contract_version"] or \
                   parent["base_commit"] != data["base_commit"] or parent["head_commit"] == data["head_commit"] or \
                   subprocess.run(["git", "merge-base", "--is-ancestor", parent["head_commit"], data["head_commit"]],
                                  cwd=checkout, capture_output=True, timeout=30).returncode:
                    fail(400, "revision does not descend from parent")
        candidate_id = new_id()
        conn.execute("""INSERT INTO candidates(id,run_id,attempt_id,contract_version,base_commit,head_commit,
                        artifact_sha256,check_hash,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                     (candidate_id, row["run_id"], row["id"], row["contract_version"], data["base_commit"],
                      data["head_commit"], artifact["sha256"], contract["check_hash"], canonical(evidence)))
        event(conn, row["run_id"], "candidate_preserved", {"attempt_id": str(row["id"]), "head_commit": data["head_commit"]})
        return candidate_id

    def save_review(self, conn, row, data, disposition, stale_reason=None):
        context = row["review_context"]
        if not context or row["input_candidate_id"] != row["current_candidate_id"]:
            disposition, stale_reason = "stale", "candidate is no longer current"
        raw = data.get("raw_output", "")
        error = data.get("validation_error")
        if not isinstance(raw, str):
            raw = ""
            error = "review output must be text"
        raw_bytes = raw.encode()
        if error is not None and (not isinstance(error, str) or not error.strip()):
            error = "invalid review execution diagnostic"
        if len(raw_bytes) > MAX_OUTPUT_BYTES:
            raw = raw_bytes[:MAX_OUTPUT_BYTES].decode(errors="ignore")
            error = "review output exceeds cap"
        result = None
        if not error:
            try:
                result = validate_result(raw, context, row["body"])
            except (ValueError, TypeError, KeyError) as exc:
                error = str(exc)[:500]
        provenance = data.get("provenance")
        reviewer = row["body"]["reviewer"]
        if not isinstance(provenance, dict) or set(provenance) != {"runner_identity", "instructions_sha256",
               "context_sha256", "started_at", "ended_at"} or \
           provenance.get("runner_identity") != reviewer["identity"] or \
           provenance.get("instructions_sha256") != reviewer["instructions_sha256"] or \
           provenance.get("context_sha256") != context["sha256"]:
            error = "review provenance mismatch"
        else:
            try:
                started = datetime.fromisoformat(provenance["started_at"])
                ended = datetime.fromisoformat(provenance["ended_at"])
                if started.tzinfo is None or ended.tzinfo is None or started > ended:
                    raise ValueError
            except (ValueError, TypeError):
                error = "review invocation times invalid"
        if error:
            disposition = "invalid"
            result = None
        elif stale_reason:
            error = stale_reason
        conn.execute("""INSERT INTO reviews(attempt_id,raw_output,result,validation_error,provenance,disposition)
                        VALUES (%s,%s,%s::jsonb,%s,%s::jsonb,%s)""",
                     (row["id"], raw, canonical(result) if result else None, error,
                      canonical(provenance if isinstance(provenance, dict) else {}), disposition))
        event(conn, row["run_id"], "review_preserved", {"attempt_id": str(row["id"]),
                                                        "disposition": disposition,
                                                        "verdict": result["verdict"] if result else None})
        return result, error

    def validate_readiness(self, conn, row):
        candidate = conn.execute("""SELECT c.*,a.path,a.sha256,p.status AS producer_status
                                    FROM candidates c JOIN artifacts a ON a.attempt_id=c.attempt_id
                                    JOIN attempts p ON p.id=c.attempt_id WHERE c.id=%s""",
                                 (row["input_candidate_id"],)).fetchone()
        if not candidate or candidate["run_id"] != row["run_id"] or \
           candidate["contract_version"] != row["contract_version"] or \
           candidate["id"] != row["current_candidate_id"] or \
           candidate["producer_status"] != "candidate" or \
           candidate["check_hash"] != row["body"]["check_hash"] or \
           candidate["evidence"]["base_commit"] != candidate["base_commit"] or \
           candidate["evidence"]["head_commit"] != candidate["head_commit"] or \
           candidate["evidence"]["check_hash"] != row["body"]["check_hash"] or \
           any(check["exit_code"] != 0 for check in candidate["evidence"]["checks"]):
            raise ReviewInputError("candidate does not qualify for readiness")
        review = conn.execute("SELECT disposition,result FROM reviews WHERE attempt_id=%s", (row["id"],)).fetchone()
        if not review or review["disposition"] != "accepted" or review["result"]["verdict"] != "pass" or \
           row["cancel_requested"] or row["deadline"] <= datetime.now(timezone.utc) or \
           row["run_status"] != "reviewing":
            raise ReviewInputError("review does not qualify for readiness")
        unresolved = conn.execute("""SELECT 1 FROM attempts a JOIN tasks t ON t.id=a.task_id
                                     WHERE t.run_id=%s AND a.id<>%s AND a.status IN ('running','uncertain')
                                     LIMIT 1""", (row["run_id"], row["id"])).fetchone()
        conflict = conn.execute("""SELECT 1 FROM events WHERE run_id=%s AND kind='conflicting_report'
                                   AND payload->>'attempt_id' IN (%s,%s) LIMIT 1""",
                                (row["run_id"], str(candidate["attempt_id"]), str(row["id"]))).fetchone()
        if unresolved or conflict:
            raise ReviewInputError("current evidence is unresolved or conflicting")
        try:
            data = (self.app.artifact_dir / candidate["path"]).read_bytes()
        except OSError:
            data = b""
        if hashlib.sha256(data).hexdigest() != candidate["sha256"]:
            raise ReviewInputError("candidate artifact integrity failure")
        context = row["review_context"]
        if context["sha256"] != digest(context["pack"]) or \
           context["pack"]["candidate_id"] != str(candidate["id"]) or \
           context["pack"]["bundle_sha256"] != candidate["sha256"] or \
           context["pack"]["contract_sha256"] != digest(row["body"]) or \
           context["pack"]["runner_identity"] != row["body"]["reviewer"]["identity"] or \
           context["pack"]["check_hash"] != row["body"]["check_hash"] or \
           context["pack"]["check_evidence"] != candidate["evidence"]:
            raise ReviewInputError("review context does not qualify")


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
        self.stopping = threading.Event()
        speech_config = getattr(args, "speech_config", None)
        speech_scratch = getattr(args, "speech_scratch", None)
        if bool(speech_config) != bool(speech_scratch):
            raise ValueError("speech configuration and scratch path must be provided together")
        self.speech = SpeechRunner(speech_config, speech_scratch) if speech_config else None
        self.vapid_private_key = getattr(args, "vapid_private_key", None)
        self.vapid_subject = getattr(args, "vapid_subject", None)
        if bool(self.vapid_private_key) != bool(self.vapid_subject):
            raise ValueError("VAPID key and subject must be provided together")
        self.vapid_public_key = vapid_public_key(self.vapid_private_key) if self.vapid_private_key else None
        github_config = github.load_config(args.github_config)
        self.github_owners, self.github_token = github_config["owners"], github_config["token"]
        self.git_base = github.GIT_BASE
        # A rebuildable cache of GitHub repositories; it holds no authoritative state.
        self.mirror_dir = self.artifact_dir / "mirrors"
        self.mirror_dir.mkdir(exist_ok=True, mode=0o700)
        decider_config = getattr(args, "decider_config", None)
        self.decider_api = decider.API
        self.decider = decider.load_config(decider_config) if decider_config else None
        telegram_config = getattr(args, "telegram_config", None)
        self.telegram_api = telegram.API
        self.telegram_token = self.telegram_user_id = None
        if telegram_config:
            config = telegram.load_config(telegram_config)
            self.telegram_token, self.telegram_user_id = config["token"], config["user_id"]
        reviewer_path = getattr(args, "reviewer_config", None)
        self.reviewer = json.loads(Path(reviewer_path).read_text()) if reviewer_path else None
        if not self.reviewer or set(self.reviewer) != {"identity", "instructions", "destination", "timeout"} or \
           not isinstance(self.reviewer["identity"], str) or not self.reviewer["identity"] or \
           not isinstance(self.reviewer["instructions"], str) or not self.reviewer["instructions"] or \
           self.reviewer["destination"] not in ("local", "openai") or \
           type(self.reviewer["timeout"]) is not int or \
           not 1 <= self.reviewer["timeout"] <= 1800:
            raise ValueError("invalid server reviewer configuration")

    def sweep(self):
        while not self.stopping.is_set():
            try:
                with connect(self.dsn) as conn:
                    expire_leases(conn)
                prepare_reviews(self)
            except (psycopg.Error, OSError, ReviewInputError):
                pass
            self.stopping.wait(2)

    def stop(self):
        self.stopping.set()
        if self.speech:
            self.speech.stop()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--dsn", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--dsn", required=True)
    serve.add_argument("--artifacts", required=True)
    serve.add_argument("--username", required=True)
    serve.add_argument("--password", required=True)
    serve.add_argument("--worker-id", required=True)
    serve.add_argument("--worker-token", required=True)
    serve.add_argument("--origin", required=True, help="External HTTPS origin, e.g. https://teem.example")
    serve.add_argument("--reviewer-config", required=True, help="server-owned local review identity and instructions JSON")
    serve.add_argument("--speech-config", help="server-owned whisper-cli executable, model identity, and language JSON")
    serve.add_argument("--speech-scratch", help="private temporary directory outside artifacts and backups")
    serve.add_argument("--vapid-private-key", help="stable server-owned VAPID private key PEM")
    serve.add_argument("--vapid-subject", help="VAPID contact, e.g. mailto:operator@example.com")
    serve.add_argument("--github-config", required=True, help="allowed GitHub owners and optional token JSON")
    serve.add_argument("--decider-config", help="OpenRouter API key, model, and timeout JSON")
    serve.add_argument("--telegram-config", help="server-owned bot token and allowed Telegram user_id JSON")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.dsn)
    else:
        if not all((args.password, args.worker_token, args.origin.startswith("https://"))):
            parser.error("password, worker token, and HTTPS origin required")
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.app = App(args)
        threading.Thread(target=server.app.sweep, daemon=True).start()
        if server.app.vapid_public_key:
            threading.Thread(target=sender_loop, args=(server.app,), daemon=True).start()
        if server.app.telegram_token:
            threading.Thread(target=telegram.poll_loop, args=(server.app,), daemon=True).start()
            threading.Thread(target=telegram.sender_loop, args=(server.app,), daemon=True).start()
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
