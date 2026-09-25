"""Build and validate the exact, bounded input to an independent review."""

import json
import subprocess
import tempfile

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


def _line_counts(repo, revision):
    """Line counts of the text files at a revision; reviewers cite source by these bounds."""
    counts = {}
    # -I skips binary files; -z puts a NUL between the path and its count.
    result = subprocess.run(["git", "grep", "-I", "-z", "-c", "", revision], cwd=repo, capture_output=True,
                            timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1",
                                              "GIT_CONFIG_GLOBAL": "/dev/null"})
    if result.returncode not in (0, 1):
        raise ReviewInputError("candidate bundle cannot be inspected")
    for record in result.stdout.splitlines():
        name, count = record.split(b"\0", 1)
        counts[name.split(b":", 1)[1].decode("utf-8", "surrogateescape")] = int(count)
    return counts


def build_context(bundle_path, candidate, contract, reviewer):
    """The frozen review input. Reviewers read source from a disposable checkout of the Candidate."""
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
        files = _line_counts(checkout, head)
        diff = _git(checkout, "diff", "--no-ext-diff", "--no-textconv", base, head).decode("utf-8", "replace")
    if len(diff.encode()) > MAX_REVIEW_INPUT_BYTES // 2:
        raise ReviewInputError("candidate diff exceeds review input cap")
    pack = {"run_id": str(candidate["run_id"]), "candidate_id": str(candidate["id"]),
            "contract_version": candidate["contract_version"], "contract_sha256": digest(contract),
            "objective": contract["objective"], "acceptance_criteria": contract["acceptance_criteria"],
            "base_commit": base, "head_commit": head, "bundle_sha256": candidate["artifact_sha256"],
            "checks": contract["checks"], "check_hash": contract["check_hash"],
            "check_evidence": candidate["evidence"], "review_instructions": reviewer["instructions"],
            "runner_identity": reviewer["identity"], "diff": diff, "files": files}
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
        if not isinstance(finding, dict) or \
           set(finding) not in ({"criterion", "description", "evidence"},
                                {"criterion", "description", "evidence", "reproduction"}) or \
           finding["criterion"] not in criteria or not isinstance(finding["description"], str) or \
           not finding["description"].strip() or not isinstance(finding["evidence"], list) or \
           not finding["evidence"]:
            raise ValueError("review finding invalid")
        reproduction = finding.get("reproduction")
        if reproduction is not None and (
                not isinstance(reproduction, dict) or set(reproduction) != {"path", "content", "output"} or
                not isinstance(reproduction["path"], str) or not 0 < len(reproduction["path"]) <= 200 or
                not isinstance(reproduction["content"], str) or not 0 < len(reproduction["content"]) <= 8000 or
                not isinstance(reproduction["output"], str) or len(reproduction["output"]) > 2000):
            raise ValueError("review reproduction invalid")
        for ref in finding["evidence"]:
            if not isinstance(ref, dict):
                raise ValueError("review reference invalid")
            if ref.get("kind") == "check" and set(ref) == {"kind", "name"}:
                if ref["name"] not in {check["name"] for check in contract["checks"]}:
                    raise ValueError("unknown check reference")
            elif ref.get("kind") == "source" and set(ref) == {"kind", "path", "start_line", "end_line"}:
                lines = pack["files"].get(ref["path"])
                if not lines or type(ref["start_line"]) is not int or type(ref["end_line"]) is not int or \
                   not 1 <= ref["start_line"] <= ref["end_line"] <= lines:
                    raise ValueError("source reference out of bounds")
            else:
                raise ValueError("review reference invalid")
    return result
