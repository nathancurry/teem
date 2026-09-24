"""Build and validate the exact, bounded input to an independent review."""

import json
import subprocess
import tempfile
from pathlib import Path

from .common import MAX_REVIEW_INPUT_BYTES, canonical, digest


class ReviewInputError(ValueError):
    pass


def _git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=120,
                            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1",
                                 "GIT_CONFIG_GLOBAL": "/dev/null"})
    if result.returncode:
        raise ReviewInputError("candidate bundle cannot be inspected")
    return result.stdout


def _tree(repo, revision):
    entries = {}
    for record in _git(repo, "ls-tree", "-rz", revision).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, kind, sha = metadata.split()
            entries[path.decode("utf-8", "surrogateescape")] = (mode, kind, sha)
    return entries


def build_context(bundle_path, candidate, contract, reviewer):
    with tempfile.TemporaryDirectory() as checkout:
        clone = subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(bundle_path), checkout],
                               capture_output=True, timeout=120,
                               env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1",
                                    "GIT_CONFIG_GLOBAL": "/dev/null"})
        if clone.returncode:
            raise ReviewInputError("candidate bundle cannot be opened")
        base, head = candidate["base_commit"], candidate["head_commit"]
        if _git(checkout, "rev-parse", "HEAD").strip().decode() != head:
            raise ReviewInputError("candidate bundle HEAD mismatch")
        base_tree, head_tree = _tree(checkout, base), _tree(checkout, head)
        sources, omissions = {}, []
        source_bytes = 0
        for path in sorted(set(base_tree) | set(head_tree)):
            entry = head_tree.get(path) or base_tree[path]
            changed = base_tree.get(path) != head_tree.get(path)
            if entry[1] != b"blob":
                if changed:
                    raise ReviewInputError("changed submodule cannot be reviewed")
                omissions.append({"path": path, "reason": "submodule", "object": entry[2].decode(),
                                  "mode": entry[0].decode()})
                continue
            if int(_git(checkout, "cat-file", "-s", entry[2].decode())) > MAX_REVIEW_INPUT_BYTES:
                raise ReviewInputError("source exceeds review input cap")
            content = _git(checkout, "cat-file", "blob", entry[2].decode())
            source_bytes += len(content)
            if source_bytes > MAX_REVIEW_INPUT_BYTES:
                raise ReviewInputError("source exceeds review input cap")
            try:
                if b"\0" in content:
                    raise UnicodeError
                source = content.decode("utf-8")
            except UnicodeError:
                if changed:
                    raise ReviewInputError("changed binary cannot be reviewed")
                omissions.append({"path": path, "reason": "nontext", "object": entry[2].decode(),
                                  "mode": entry[0].decode()})
                continue
            sources[path] = {"version": "head" if path in head_tree else "base_deleted",
                             "object": entry[2].decode(), "mode": entry[0].decode(),
                             "lines": source.splitlines()}
        diff = _git(checkout, "diff", "--no-ext-diff", "--no-textconv", base, head).decode("utf-8", "replace")
    pack = {"run_id": str(candidate["run_id"]), "candidate_id": str(candidate["id"]),
            "contract_version": candidate["contract_version"], "contract_sha256": digest(contract),
            "objective": contract["objective"], "acceptance_criteria": contract["acceptance_criteria"],
            "base_commit": base, "head_commit": head, "bundle_sha256": candidate["artifact_sha256"],
            "checks": contract["checks"], "check_hash": contract["check_hash"],
            "check_evidence": candidate["evidence"], "review_instructions": reviewer["instructions"],
            "runner_identity": reviewer["identity"], "diff": diff, "sources": sources,
            "omissions": omissions}
    if len(canonical(pack).encode()) > MAX_REVIEW_INPUT_BYTES:
        raise ReviewInputError("review input exceeds cap")
    return {"pack": pack, "sha256": digest(pack)}


def validate_result(raw, context, contract):
    if not isinstance(raw, str):
        raise ValueError("review output must be text")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate review key")
            result[key] = value
        return result

    result = json.loads(raw, object_pairs_hook=unique)
    pack = context["pack"]
    keys = {"candidate_id", "contract_version", "context_sha256", "verdict", "summary",
            "findings", "uncertainties"}
    if not isinstance(result, dict) or set(result) != keys:
        raise ValueError("review object keys mismatch")
    if result["candidate_id"] != pack["candidate_id"] or type(result["contract_version"]) is not int or \
       result["contract_version"] != pack["contract_version"] or result["context_sha256"] != context["sha256"]:
        raise ValueError("review assignment identity mismatch")
    verdict = result["verdict"]
    findings, uncertainties = result["findings"], result["uncertainties"]
    if verdict not in ("pass", "changes_required", "blocked") or \
       not isinstance(result["summary"], str) or not result["summary"].strip() or \
       not isinstance(findings, list) or not isinstance(uncertainties, list) or \
       any(not isinstance(x, str) or not x.strip() for x in uncertainties):
        raise ValueError("review verdict or text invalid")
    if (verdict == "pass" and (findings or uncertainties)) or \
       (verdict == "changes_required" and (not findings or uncertainties)) or \
       (verdict == "blocked" and not uncertainties):
        raise ValueError("review verdict contents invalid")
    criteria = {contract["objective"], contract["acceptance_criteria"]}
    criteria.update(line.strip() for line in contract["acceptance_criteria"].splitlines() if line.strip())
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"criterion", "description", "evidence"} or \
           finding["criterion"] not in criteria or not isinstance(finding["description"], str) or \
           not finding["description"].strip() or not isinstance(finding["evidence"], list) or \
           not finding["evidence"]:
            raise ValueError("review finding invalid")
        for ref in finding["evidence"]:
            if not isinstance(ref, dict):
                raise ValueError("review reference invalid")
            if ref.get("kind") == "check" and set(ref) == {"kind", "name"}:
                if ref["name"] not in {check["name"] for check in contract["checks"]}:
                    raise ValueError("unknown check reference")
            elif ref.get("kind") == "source" and set(ref) == {"kind", "path", "start_line", "end_line"}:
                source = pack["sources"].get(ref["path"])
                if not source or type(ref["start_line"]) is not int or type(ref["end_line"]) is not int or \
                   not 1 <= ref["start_line"] <= ref["end_line"] <= len(source["lines"]):
                    raise ValueError("source reference out of bounds")
            else:
                raise ValueError("review reference invalid")
    return result
