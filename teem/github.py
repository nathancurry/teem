import base64
import hashlib
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .common import COMMIT_RE, REPO_RE, check_config
from .db import connect, event
from .workflow import stop_run


GIT_BASE = "https://github.com"
API = "https://api.github.com"
PUBLISH_ATTEMPTS = 6


class GitHubError(Exception):
    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def load_config(path):
    with open(path) as source:
        config = json.load(source)
    if not isinstance(config, dict) or not {"owners"} <= set(config) <= {"owners", "token"} or \
       not isinstance(config["owners"], list) or not config["owners"] or \
       any(not isinstance(owner, str) or not REPO_RE.fullmatch(owner.lower() + "/x") for owner in config["owners"]) or \
       not isinstance(config.get("token", ""), str):
        raise ValueError("GitHub configuration needs owners and an optional token")
    return {"owners": [owner.lower() for owner in config["owners"]], "token": config.get("token")}


def normalize_repo(repo, owners):
    """Return the lowercase owner/name if it is well formed and inside the owner allowlist."""
    if not isinstance(repo, str):
        return None
    repo = repo.strip().lower().removesuffix(".git")
    if not REPO_RE.fullmatch(repo) or repo.split("/")[0] not in owners or ".." in repo:
        return None
    return repo


def git(args, token=None, cwd=None, timeout=30):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null"}
    if token:
        # Passed through the environment so the token never appears in process arguments.
        basic = base64.b64encode(("x-access-token:" + token).encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "http.extraHeader",
                    "GIT_CONFIG_VALUE_1": "Authorization: Basic " + basic})
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitHubError(f"git {args[0]} timed out") from None
    if result.returncode:
        raise GitHubError(f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
    return result.stdout


def update_mirror(app, repo):
    mirror = app.mirror_dir / (repo.replace("/", "__") + ".git")
    if mirror.exists():
        git(["fetch", "--prune", "origin"], app.github_token, cwd=mirror, timeout=300)
    else:
        git(["clone", "--mirror", "--quiet", f"{app.git_base}/{repo}.git", str(mirror)], app.github_token, timeout=600)
    return mirror


def fetch_base(app, repo):
    """Update the server mirror; return the default-branch head and its declared checks."""
    mirror = update_mirror(app, repo)
    base = git(["rev-parse", "HEAD^{commit}"], cwd=mirror).strip()
    if not COMMIT_RE.fullmatch(base):
        raise GitHubError("default branch has no commit")
    try:
        git(["cat-file", "-e", base + ":.teem/checks.json"], cwd=mirror)
    except GitHubError:
        return base, []
    text = git(["show", base + ":.teem/checks.json"], cwd=mirror)
    try:
        return base, check_config(json.loads(text))
    except ValueError as exc:
        raise GitHubError(f".teem/checks.json is invalid: {exc}") from None


def api(app, method, path, body=None):
    request = urllib.request.Request(app.github_api + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + app.github_token,
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "teem",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(4 * 1024 * 1024))
    except urllib.error.HTTPError as exc:
        # Client errors other than rate limiting will not succeed on retry.
        raise GitHubError(f"GitHub {method} returned {exc.code}",
                          permanent=400 <= exc.code < 500 and exc.code not in (403, 429)) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GitHubError(f"GitHub unavailable: {type(exc).__name__}") from None


def pull_request_body(app, run):
    contract = run["body"]
    checks = "\n".join(f"- {c['name']}: {'passed' if c['exit_code'] == 0 else 'failed'}"
                       for c in run["evidence"].get("checks", [])) or "- None configured (.teem/checks.json)"
    review = run["review"] or {}
    unresolved = ""
    if run["unreviewed"]:
        items = [f"- {f['description']}" for f in review.get("findings", [])] + \
                [f"- Unsure: {u}" for u in review.get("uncertainties", [])]
        unresolved = ("\n\n**This pull request did not pass independent review.** The user chose to publish it "
                      "anyway. Unresolved review findings:\n\n" + ("\n".join(items) or "- None recorded"))
    return (f"**Objective**\n\n{contract['objective']}\n\n**Acceptance criteria**\n\n{contract['acceptance_criteria']}"
            f"\n\n**Checks**\n\n{checks}\n\n**Implementer summary**\n\n{run['summary'] or 'None reported.'}"
            f"\n\n**Independent review**: {review.get('verdict', 'unknown')}. {review.get('summary', '')}"
            f"{unresolved}\n\nTeem run: {app.origin}/runs/{run['id']}")


def open_pull_request(app, run):
    """Push the exact reviewed Candidate and open its PR. Both steps are safe to repeat."""
    if not app.github_token:
        raise GitHubError("no GitHub token is configured", permanent=True)
    bundle = app.artifact_dir / run["path"]
    try:
        intact = hashlib.sha256(bundle.read_bytes()).hexdigest() == run["artifact_sha256"]
    except OSError:
        intact = False
    if not intact:
        raise GitHubError("candidate artifact integrity failure", permanent=True)
    repo, branch = run["project_id"], f"teem/{run['id']}"
    mirror = update_mirror(app, repo)
    default = git(["symbolic-ref", "--short", "HEAD"], cwd=mirror).strip()
    git(["fetch", "--quiet", str(bundle), "HEAD"], cwd=mirror, timeout=300)
    # Push to the URL rather than the mirror's origin, whose mirror setting forbids explicit refspecs.
    git(["push", "--force", "--quiet", f"{app.git_base}/{repo}.git", f"{run['head_commit']}:refs/heads/{branch}"],
        app.github_token, cwd=mirror, timeout=600)
    owner = repo.split("/")[0]
    query = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "all"})
    existing = api(app, "GET", f"/repos/{repo}/pulls?{query}")
    if existing:
        return existing[0]["html_url"]
    title = ("Teem (review not passed): " if run["unreviewed"] else "Teem: ") + \
        run["body"]["objective"].splitlines()[0][:70]
    try:
        return api(app, "POST", f"/repos/{repo}/pulls", {"title": title, "head": branch, "base": default,
                                                         "body": pull_request_body(app, run)})["html_url"]
    except GitHubError:
        # A concurrent or earlier unrecorded create leaves an existing PR; use it if present.
        existing = api(app, "GET", f"/repos/{repo}/pulls?{query}")
        if existing:
            return existing[0]["html_url"]
        raise


def publish_due(app):
    with connect(app.dsn) as conn:
        run = conn.execute("""SELECT r.id,r.project_id,r.publish_attempts,c.body,x.head_commit,x.artifact_sha256,
                              x.evidence,a.path,p.status AS project_status,u.usage->>'summary' AS summary,
                              (SELECT source FROM approvals WHERE run_id=r.id AND decision='approve'
                               AND action='code_check_review' ORDER BY created_at DESC LIMIT 1) AS approval_source,
                              EXISTS (SELECT 1 FROM approvals WHERE run_id=r.id AND action='publish_unreviewed')
                               AS unreviewed,
                              (SELECT v.result FROM reviews v JOIN attempts ra ON ra.id=v.attempt_id
                               JOIN tasks t ON t.id=ra.task_id WHERE t.input_candidate_id=x.id
                               AND v.disposition='accepted' ORDER BY v.created_at DESC LIMIT 1) AS review
                              FROM runs r JOIN contracts c ON c.run_id=r.id AND c.version=r.contract_version
                              JOIN candidates x ON x.id=r.current_candidate_id
                              JOIN artifacts a ON a.attempt_id=x.attempt_id
                              JOIN attempts u ON u.id=x.attempt_id JOIN projects p ON p.id=r.project_id
                              WHERE r.status='ready_to_merge' AND (r.next_publish_at IS NULL OR r.next_publish_at<=now())
                              ORDER BY r.updated_at FOR UPDATE OF r SKIP LOCKED LIMIT 1""").fetchone()
        if not run:
            return False
        if run["approval_source"].startswith("project_grant:") and run["project_status"] != "granted":
            stop_run(conn, run["id"], "grant_revoked")
            return True
        attempts = run["publish_attempts"] + 1
        conn.execute("""UPDATE runs SET publish_attempts=%s,
                        next_publish_at=now()+(%s || ' seconds')::interval WHERE id=%s""",
                     (attempts, 30 * 2 ** attempts, run["id"]))
    try:
        url = open_pull_request(app, run)
    except GitHubError as exc:
        if exc.permanent or attempts >= PUBLISH_ATTEMPTS:
            with connect(app.dsn) as conn:
                if conn.execute("SELECT status FROM runs WHERE id=%s FOR UPDATE", (run["id"],)).fetchone()["status"] == "ready_to_merge":
                    stop_run(conn, run["id"], f"publish_failed: {exc}"[:500])
        return True
    with connect(app.dsn) as conn:
        if conn.execute("""UPDATE runs SET status='pr_open',pr_url=%s,updated_at=now()
                           WHERE id=%s AND status='ready_to_merge' RETURNING id""", (url, run["id"])).fetchone():
            event(conn, run["id"], "pull_request_opened", {"url": url}, notify=True)
    return True


def publisher_loop(app):
    while not app.stopping.is_set():
        try:
            if publish_due(app):
                continue
        except Exception as exc:
            print(f"publisher: {type(exc).__name__}: {exc}", flush=True)
        app.stopping.wait(5)
