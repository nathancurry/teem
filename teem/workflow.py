"""Controller transitions shared by the web UI, Telegram buttons, and the decider."""

import hashlib

from .common import RUN_SECONDS, STATUS_LABELS, canonical, digest, fail, new_id
from .db import event


def reviewer_identity(config):
    result = {"identity": config["identity"],
              "instructions_sha256": hashlib.sha256(config["instructions"].encode()).hexdigest(),
              "destination": config["destination"], "timeout": config["timeout"]}
    if config["destination"] == "local-ollama":
        result["model"] = config["model"]
    return result


def create_run(conn, project_id, base, checks, reviewer, objective, criteria, dedupe_key, revisions=2):
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
    # FOR SHARE makes a concurrent revocation wait until this Run's authorization is decided.
    project = conn.execute("SELECT * FROM projects WHERE id=%s FOR SHARE", (project_id,)).fetchone()
    if not project:
        fail(400, "unknown project")
    request_id, run_id = new_id(), new_id()
    body = {"objective": objective, "acceptance_criteria": criteria, "project_id": project_id,
            "base_commit": base, "context_version": 1,
            "allowed_actions": ["code", "check", "review", "revise"], "checks": checks,
            "check_hash": digest(checks), "reviewer": reviewer_identity(reviewer),
            "limits": {"seconds": RUN_SECONDS, "attempts": 8, "revisions": revisions},
            "delivery_condition": "ready_to_merge after passing checks and independent passing review"}
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
    event(conn, run_id, "proposal_created", {"contract_version": 1, "request_id": request_id}, notify=not granted)
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


def cancel_run(conn, run_id):
    run = conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
    if not run:
        fail(404, "run not found")
    if run["status"] not in ("queued", "coding", "awaiting_review", "reviewing", "uncertain"):
        fail(409, "run cannot be cancelled")
    pending = run["status"] in ("coding", "reviewing", "uncertain")
    status = "cancelling" if pending else "cancelled"
    conn.execute("UPDATE runs SET status=%s,updated_at=now() WHERE id=%s", (status, run_id))
    conn.execute("""UPDATE tasks SET cancel_requested=true,status=%s WHERE run_id=%s
                    AND status IN ('queued','running','uncertain')""", (status, run_id))
    event(conn, run_id, "cancel_requested", {})
    return run


def status_rows(conn, run_id=None):
    rows = conn.execute("""SELECT r.id,r.project_id,p.name AS project_name,r.status,r.stop_reason,
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
                       "stop_reason": row["stop_reason"], "updated_at": row["updated_at"].isoformat()})
    return result
