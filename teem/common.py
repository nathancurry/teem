import hashlib
import json
import re
import uuid

PROTOCOL = 1
LEASE_SECONDS = 45
RUN_SECONDS = 1800
CODER_SECONDS = 900
CHECK_SECONDS = 300
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def new_id():
    return str(uuid.uuid4())


def check_config(checks):
    if not isinstance(checks, list) or not checks:
        raise ValueError("at least one objective check is required")
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
