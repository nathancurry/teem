import hashlib
import json
import re
import uuid

PROTOCOL = 2
LEASE_SECONDS = 45
RUN_SECONDS = 1800
CODER_SECONDS = 900
CHECK_SECONDS = 300
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
MAX_REVIEW_INPUT_BYTES = 24 * 1024
REVIEW_SECONDS = 300
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9._-]+$")

STATUS_LABELS = {"awaiting_approval": "Decision required", "queued": "Queued",
                 "coding": "Coding and checks", "awaiting_review": "Awaiting review",
                 "reviewing": "Independent review", "uncertain": "Execution unresolved",
                 "cancelling": "Cancellation unresolved", "failed": "Execution failed",
                 "checks_failed": "Checks failed", "blocked": "Stopped",
                 "ready_to_merge": "Ready to merge", "denied": "Denied", "cancelled": "Cancelled"}


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def fail(status, message):
    raise ApiError(status, message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def new_id():
    return str(uuid.uuid4())


def check_config(checks):
    if not isinstance(checks, list):
        raise ValueError("checks must be a list")
    names = set()
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"name", "argv"}:
            raise ValueError("checks need name and argv")
        name, argv = item["name"], item["argv"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("check names must be unique")
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
            raise ValueError("check argv must be a nonempty string list")
        names.add(name)
    return checks
