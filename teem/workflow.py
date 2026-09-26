"""Controller transitions shared by the web UI, Telegram buttons, and the decider."""

import hashlib

from .common import IMPLEMENTER_MODELS, RUN_SECONDS, STATUS_LABELS, canonical, digest, fail, new_id
from .db import event


def reviewer_identity(config):
    return {"identity": config["identity"],
            "instructions_sha256": hashlib.sha256(config["instructions"].encode()).hexdigest(),
            "destination": config["destination"], "timeout": config["timeout"]}


def create_run(conn, project_id, base, checks, reviewer, objective, criteria, dedupe_key, revisions=2, model=None,
               decider_model=None):
    """Create a proposed Run, or return the Run already created for this deduplication key.

    A granted project authorizes the Run in the same transaction; otherwise the Run waits for a
    decision. Returns (run_id, created). The caller commits.
    """
    existing = conn.execute("""SELECT r.id FROM runs r JOIN requests q ON q.id=r.request_id
                               WHERE q.dedupe_key=%s""", (dedupe_key,)).fetchone()
    if existing:
        return str(existing["id"]), False
    if not objective or not criteria or len(objective) > 8000 or len(criteria) > 8000:
        fail(400, "objective and acceptance criteria required")
    if revisions not in (0, 1, 2):
        fail(400, "revision limit must be 0–2")
    if model is not None and model not in IMPLEMENTER_MODELS:
        fail(400, "unknown implementer model")
    # FOR SHARE makes a concurrent revocation wait until this Run's authorization is decided.
    project = conn.execute("SELECT * FROM projects WHERE id=%s FOR SHARE", (project_id,)).fetchone()
    if not project:
        fail(400, "unknown project")
    request_id, run_id = new_id(), new_id()
    body = {"objective": objective, "acceptance_criteria": criteria, "project_id": project_id,
            "base_commit": base, "context_version": 1,
            "allowed_actions": ["code", "check", "review", "revise", "publish_pr"], "checks": checks,
            "check_hash": digest(checks), "reviewer": reviewer_identity(reviewer),
            "limits": {"seconds": RUN_SECONDS, "attempts": 8, "revisions": revisions},
            "delivery_condition": "pull request from a teem/ branch after passing checks and independent passing review"}
    if model:
        # Part of the approved scope: a stronger model spends more of the subscription's limits.
        body["implementer_model"] = model
    proposal = {"repository": project["name"], "base_commit": base,
                "allowed_actions": body["allowed_actions"], "check_plan": checks,
                "limits": body["limits"], "delivery_condition": body["delivery_condition"]}
    inserted = conn.execute("""INSERT INTO requests(id,dedupe_key,text,classification)
                               VALUES (%s,%s,%s,'proposal') ON CONFLICT (dedupe_key) DO NOTHING
                               RETURNING id""",
                            (request_id, dedupe_key, objective + "\n\nAcceptance criteria:\n" + criteria)).fetchone()
    if not inserted:
        fail(409, "request is still being created")
    conn.execute("""INSERT INTO runs(id,request_id,project_id,status,deadline)
                    VALUES (%s,%s,%s,'awaiting_approval',now()+(%s || ' seconds')::interval)""",
                 (run_id, request_id, project_id, RUN_SECONDS))
    conn.execute("INSERT INTO contracts(run_id,version,body,proposal) VALUES (%s,1,%s::jsonb,%s::jsonb)",
                 (run_id, canonical(body), canonical(proposal)))
    granted = project["status"] == "granted"
    event(conn, run_id, "proposal_created", {"contract_version": 1, "request_id": request_id,
                                             "decider_model": decider_model}, notify=not granted)
    if granted:
        decide_run(conn, run_id, 1, "approve", "project_grant:" + str(project["grant_id"]))
    return run_id, True


def decide_run(conn, run_id, version, decision, source):
    run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
    if not run:
        fail(404, "run not found")
    if run["status"] != "awaiting_approval" or str(version) != str(run["contract_version"]):
        fail(409, "decision is stale")
    if decision not in ("approve", "deny"):
        fail(400, "specific decision required")
    conn.execute("""INSERT INTO approvals(id,run_id,contract_version,action,decision,source,target,target_state)
                    VALUES (%s,%s,%s,'code_check_review',%s,%s,%s,'awaiting_approval')""",
                 (new_id(), run_id, run["contract_version"], decision, source, run["project_id"]))
    event(conn, run_id, "approval_decided", {"action": "code_check_review", "decision": decision, "source": source})
    if decision == "approve":
        conn.execute("""UPDATE runs SET status='queued',updated_at=now(),
                        deadline=now()+(%s || ' seconds')::interval WHERE id=%s""", (RUN_SECONDS, run_id))
        conn.execute("INSERT INTO tasks(id,run_id,kind,status) VALUES (%s,%s,'code_and_check','queued')", (new_id(), run_id))
        event(conn, run_id, "task_queued", {"kind": "code_and_check"})
    else:
        conn.execute("UPDATE runs SET status='denied',updated_at=now() WHERE id=%s", (run_id,))
    return run


def stop_run(conn, run_id, reason, kind="blocked"):
    prior = conn.execute("SELECT status FROM runs WHERE id=%s", (run_id,)).fetchone()["status"]
    conn.execute("UPDATE runs SET status=%s,stop_reason=%s,updated_at=now() WHERE id=%s", (kind, reason, run_id))
    event(conn, run_id, "run_stopped", {"status": kind, "reason": reason},
          notify=prior != kind and kind in ("blocked", "failed", "checks_failed", "uncertain"))


CANCELLABLE = ("queued", "coding", "awaiting_review", "reviewing", "uncertain")


def cancel_run(conn, run_id, reviewer):
    """Request cancellation. A Run with no execution in flight is cancelled at once; otherwise it
    waits in `cancelling` until the worker confirms the stop. Returns the Run's new status."""
    run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
    if not run:
        fail(404, "run not found")
    if run["status"] not in CANCELLABLE:
        fail(409, "run cannot be cancelled")
    pending = run["status"] in ("coding", "reviewing", "uncertain")
    status = "cancelling" if pending else "cancelled"
    conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (status, run_id))
    conn.execute("""UPDATE tasks SET cancel_requested=true,status=%s WHERE run_id=%s
                    AND status IN ('queued','running','uncertain')""", (status, run_id))
    event(conn, run_id, "cancel_requested", {})
    if status == "cancelled":
        run_cancelled(conn, run_id, reviewer)
    return status


def request_replacement(conn, run_id, proposal, reviewer):
    """Stop a Run and start `proposal` in its place once the stop is confirmed.

    The proposal is kept in the event log, so a replacement survives restarts while the old Run
    finishes cancelling. Returns the old Run's status after the request.
    """
    event(conn, run_id, "replacement_requested", proposal)
    return cancel_run(conn, run_id, reviewer)


def run_cancelled(conn, run_id, reviewer):
    """Called in the transaction that makes a Run `cancelled`: confirm it and start any replacement."""
    event(conn, run_id, "cancel_confirmed", {}, notify=True)
    pending = conn.execute("""SELECT payload FROM events WHERE run_id=%s AND kind='replacement_requested'
                              ORDER BY id DESC LIMIT 1""", (run_id,)).fetchone()
    if not pending:
        return None
    proposal = pending["payload"]
    new_run, created = create_run(conn, proposal["project_id"], proposal["base"], proposal["checks"], reviewer,
                                  proposal["objective"], proposal["criteria"], "replace:" + str(run_id),
                                  proposal["revisions"], proposal.get("model"))
    if created and conn.execute("SELECT status FROM runs WHERE id=%s", (new_run,)).fetchone()["status"] == "queued":
        # A granted replacement starts without a decision message, so say that it started.
        event(conn, new_run, "replacement_started", {"replaces": str(run_id)}, notify=True)
    return new_run


KEEP_GOING_REVISIONS = 2
KEEP_GOING_ATTEMPTS = 4


def run_limit(conn, run_id, contract, name):
    """A Run's revision or Attempt limit: the contract's, plus the extension granted by Keep going.

    The contract itself never changes; the extension is the user's recorded decision.
    """
    extended = conn.execute("""SELECT 1 FROM approvals WHERE run_id=%s AND action='extend_revisions'
                               AND decision='approve'""", (run_id,)).fetchone()
    extra = {"revisions": KEEP_GOING_REVISIONS, "attempts": KEEP_GOING_ATTEMPTS}[name] if extended else 0
    return contract["limits"][name] + extra


def keep_going(conn, run_id, source):
    """After a Run stopped at its revision limit, grant two more rounds and revise the same Candidate
    against the last review's findings."""
    run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
    if not run:
        fail(404, "run not found")
    if run["status"] != "blocked" or run["stop_reason"] != "revision_limit":
        fail(409, "only a Run stopped at its revision limit can keep going")
    if conn.execute("SELECT 1 FROM approvals WHERE run_id=%s AND action='extend_revisions'", (run_id,)).fetchone():
        fail(409, "this Run already kept going once")
    review = conn.execute("""SELECT v.attempt_id,t.revision_number FROM reviews v JOIN attempts a ON a.id=v.attempt_id
                             JOIN tasks t ON t.id=a.task_id WHERE t.run_id=%s AND t.input_candidate_id=%s
                             AND v.disposition='accepted' AND v.result->>'verdict'='changes_required'
                             ORDER BY v.created_at DESC LIMIT 1""", (run_id, run["current_candidate_id"])).fetchone()
    if not review:
        fail(409, "no accepted review of the current Candidate asks for changes")
    record_decision(conn, run, "extend_revisions", source)
    conn.execute("""INSERT INTO tasks(id,run_id,kind,status,revision_number,input_candidate_id,source_review_attempt_id)
                    VALUES (%s,%s,'code_and_check','queued',%s,%s,%s)""",
                 (new_id(), run_id, review["revision_number"] + 1, run["current_candidate_id"], review["attempt_id"]))
    # Two more rounds need time; extend without ever shortening the deadline.
    conn.execute("""UPDATE runs SET status='queued',stop_reason=NULL,updated_at=now(),
                    deadline=GREATEST(deadline,now()+interval '2 hours') WHERE id=%s""", (run_id,))
    event(conn, run_id, "revision_queued", {"parent_candidate_id": str(run["current_candidate_id"]),
                                            "round": review["revision_number"] + 1, "reason": "keep_going"})


def task_attempt_limit(conn, task):
    """Two Attempts per Task. A review Task the user chose to retry gets two more."""
    if task["kind"] == "review" and conn.execute("""SELECT 1 FROM approvals WHERE run_id=%s
                                                    AND action='retry_review' AND decision='approve'""",
                                                 (task["run_id"],)).fetchone():
        return 4
    return 2


def record_decision(conn, run, action, source):
    # approvals is unique per (Run, contract version, action), so each of these decisions happens once.
    conn.execute("""INSERT INTO approvals(id,run_id,contract_version,action,decision,source,target,target_state)
                    VALUES (%s,%s,%s,%s,'approve',%s,%s,%s)""",
                 (new_id(), run["id"], run["contract_version"], action, source, run["project_id"], run["status"]))


def retry_review(conn, run_id, source):
    """After the reviewer failed (for example, bad credentials), review the same Candidate again
    instead of redoing the implementation."""
    run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
    if not run:
        fail(404, "run not found")
    task = conn.execute("""SELECT * FROM tasks WHERE run_id=%s ORDER BY revision_number DESC,kind DESC
                           LIMIT 1 FOR UPDATE""", (run_id,)).fetchone()
    if run["status"] != "failed" or not task or task["kind"] != "review" or task["status"] != "failed":
        fail(409, "only a Run stopped by a failed review can retry its review")
    if conn.execute("""SELECT 1 FROM attempts WHERE task_id=%s AND status IN ('running','uncertain')""",
                    (task["id"],)).fetchone():
        fail(409, "worker reconciliation required before retry")
    used = conn.execute("""SELECT count(*) AS n FROM attempts a JOIN tasks t ON t.id=a.task_id
                           WHERE t.run_id=%s""", (run_id,)).fetchone()["n"]
    body = conn.execute("SELECT body FROM contracts WHERE run_id=%s AND version=%s",
                        (run_id, run["contract_version"])).fetchone()["body"]
    if used >= run_limit(conn, run_id, body, "attempts"):
        fail(409, "the Run's attempt limit is reached")
    if conn.execute("SELECT 1 FROM approvals WHERE run_id=%s AND action='retry_review'", (run_id,)).fetchone():
        fail(409, "this review was already retried once")
    record_decision(conn, run, "retry_review", source)
    conn.execute("UPDATE tasks SET status='queued',cancel_requested=false WHERE id=%s", (task["id"],))
    # An explicit retry may come hours later; give it an hour without ever shortening the deadline.
    conn.execute("""UPDATE runs SET status='awaiting_review',stop_reason=NULL,updated_at=now(),
                    deadline=GREATEST(deadline,now()+interval '1 hour') WHERE id=%s""", (run_id,))
    event(conn, run_id, "review_retry_queued", {"source": source})


def publish_anyway(conn, run_id, source):
    """Publish a Candidate whose checks pass but whose review never passed, as the user decided."""
    run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
    if not run:
        fail(404, "run not found")
    if run["status"] != "blocked" or run["stop_reason"] not in ("revision_limit", "review_uncertain") or \
       not run["current_candidate_id"]:
        fail(409, "only a Run stopped at review can be published anyway")
    evidence = conn.execute("SELECT evidence FROM candidates WHERE id=%s", (run["current_candidate_id"],)).fetchone()
    if any(check["exit_code"] != 0 for check in evidence["evidence"].get("checks", [])):
        fail(409, "the Candidate's checks did not pass")
    record_decision(conn, run, "publish_unreviewed", source)
    conn.execute("""UPDATE runs SET status='ready_to_merge',stop_reason=NULL,next_publish_at=NULL,
                    updated_at=now() WHERE id=%s""", (run_id,))
    event(conn, run_id, "publish_unreviewed_approved", {"source": source})


def status_rows(conn, run_id=None):
    rows = conn.execute("""SELECT r.id,r.project_id,p.name AS project_name,r.status,r.stop_reason,r.pr_url,
                          r.updated_at,c.body,r.current_candidate_id,
                          COALESCE((SELECT max(t.revision_number) FROM tasks t WHERE t.run_id=r.id
                                    AND t.kind='code_and_check'),0) AS round
                          FROM runs r JOIN projects p ON p.id=r.project_id
                          JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                          WHERE (%s::uuid IS NULL OR r.id=%s::uuid)
                          ORDER BY (r.status='awaiting_approval') DESC,r.updated_at DESC LIMIT 30""",
                        (run_id, run_id)).fetchall()
    result = []
    for row in rows:
        candidate = conn.execute("SELECT evidence FROM candidates WHERE id=%s", (row["current_candidate_id"],)).fetchone() if row["current_candidate_id"] else None
        review = conn.execute("""SELECT v.result,v.disposition FROM reviews v JOIN attempts a ON a.id=v.attempt_id
                               JOIN tasks t ON t.id=a.task_id WHERE t.run_id=%s
                               ORDER BY v.created_at DESC,v.attempt_id DESC LIMIT 1""", (row["id"],)).fetchone()
        checks = candidate["evidence"].get("checks", []) if candidate else []
        summary = f"{sum(c['exit_code'] == 0 for c in checks)}/{len(checks)} checks passed" if checks else "Checks pending"
        if review:
            verdict = review["result"]["verdict"] if review["result"] else review["disposition"]
            summary += f"; review: {verdict}"
        else:
            summary += "; review pending"
        result.append({"id": str(row["id"]), "project": row["project_name"],
                       "objective": row["body"]["objective"], "status": row["status"],
                       "label": STATUS_LABELS.get(row["status"], row["status"]),
                       "round": row["round"], "summary": summary,
                       "stop_reason": row["stop_reason"], "pr_url": row["pr_url"],
                       "updated_at": row["updated_at"].isoformat()})
    return result
