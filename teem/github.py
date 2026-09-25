import base64
import json
import subprocess

from .common import COMMIT_RE, REPO_RE, check_config


GIT_BASE = "https://github.com"


class GitHubError(Exception):
    pass


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


def fetch_base(app, repo):
    """Update the server mirror; return the default-branch head and its declared checks."""
    mirror = app.mirror_dir / (repo.replace("/", "__") + ".git")
    if mirror.exists():
        git(["fetch", "--prune", "origin"], app.github_token, cwd=mirror, timeout=300)
    else:
        git(["clone", "--mirror", "--quiet", f"{app.git_base}/{repo}.git", str(mirror)], app.github_token, timeout=600)
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
