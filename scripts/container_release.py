"""Validate immutable image identities and monotonic release catalogue transitions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "sokolaidev/maf-extensions"
PREFIX = f"ghcr.io/{REPOSITORY}"
SIGNER = (
    f"https://github.com/{REPOSITORY}/.github/workflows/container-image-release.yml@refs/heads/main"
)
ISSUER = "https://token.actions.githubusercontent.com"
PROVENANCE = "https://slsa.dev/provenance/v1"
SPDX = "https://spdx.dev/Document/v2.3"
COMPLETION = f"https://github.com/{REPOSITORY}/blob/main/docs/security/container-image-releases.md#release-completion-v1"
IDENTITY_FIELDS = (
    "profile",
    "version",
    "sourceCommit",
    "sourceRef",
    "registryDigest",
    "assessedManifestDigest",
    "imageId",
    "attemptId",
)
OPTIONAL_IDENTITY_FIELDS = ("buildInputsSha256", "preparationEvidenceSha256")


def decode(raw: str | bytes) -> Any:
    """Decode JSON with no duplicate keys or non-finite numbers."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise ValueError(f"Invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def read(path: Path) -> Any:
    """Read an original JSON evidence file without accepting ambiguous values."""
    return decode(path.read_text(encoding="utf-8-sig"))


def write(path: Path, value: Any) -> None:
    """Write deterministic public evidence bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def digest(path: Path) -> str:
    """Hash the original file bytes without loading image layers into memory."""
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def require_digest(value: Any) -> str:
    """Require an explicit SHA-256 identity."""
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Expected a SHA-256 digest")
    return value


def timestamp(value: Any) -> datetime:
    """Require a timezone-aware UTC observation time."""
    if not isinstance(value, str):
        raise ValueError("Expected an observation timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("Observation time must be UTC")
    return result


def now() -> str:
    """Return an explicit UTC timestamp for durable evidence."""
    return datetime.now(UTC).isoformat()


def profiles() -> tuple[str, ...]:
    """Load the reviewed publication scope."""
    value = read(ROOT / "images/release-profiles.json")
    if (
        value["schemaVersion"] != 1
        or value["platform"] != "linux/amd64"
        or value["registryPrefix"] != PREFIX
    ):
        raise ValueError("Unsupported image release scope")
    names = value["profiles"]
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(n, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", n) for n in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("Invalid image profile catalogue")
    return tuple(names)


def version(value: Any) -> tuple[int, int, int]:
    """Accept stable SemVer release numbers without aliases or build suffixes."""
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value
    ):
        raise ValueError("Expected a stable image SemVer")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def validate_identity(candidate: dict[str, Any]) -> None:
    """Refuse unreviewed scope, mutable references and ambiguous source identities."""
    if candidate.get("profile") not in profiles():
        raise ValueError("Unrecognized release profile")
    version(candidate.get("version"))
    if not isinstance(candidate.get("sourceCommit"), str) or not re.fullmatch(
        r"[0-9a-f]{40}", candidate["sourceCommit"]
    ):
        raise ValueError("Expected a full source commit")
    if candidate.get("sourceRef") != "refs/heads/main":
        raise ValueError("Only protected main can publish images")
    for field in ("registryDigest", "assessedManifestDigest", "imageId"):
        require_digest(candidate.get(field))
    for field in OPTIONAL_IDENTITY_FIELDS:
        if field in candidate:
            require_digest(candidate[field])
    if not isinstance(candidate.get("attemptId"), str) or not re.fullmatch(
        r"[1-9][0-9]*", candidate["attemptId"]
    ):
        raise ValueError("Expected the originating workflow run ID")


def key(candidate: dict[str, Any]) -> str:
    """Name a permanently reserved profile/version pair."""
    validate_identity(candidate)
    return f"{candidate['profile']}/{candidate['version']}"


def empty_catalogue() -> dict[str, Any]:
    """Create the initial release history with no implied successful assessments."""
    return {"schemaVersion": 1, "releases": {}, "current": {}}


def validate_catalogue(value: Any) -> None:
    """Require complete release identities and consistent current/superseded pointers."""
    if (
        not isinstance(value, dict)
        or type(value.get("schemaVersion")) is not int
        or value["schemaVersion"] != 1
        or not isinstance(value.get("releases"), dict)
        or not isinstance(value.get("current"), dict)
    ):
        raise ValueError("Invalid release catalogue")
    completed_by_profile: dict[str, list[dict[str, Any]]] = {}
    for name, record in value["releases"].items():
        if not isinstance(record, dict) or key(record) != name:
            raise ValueError("Catalogue release key differs from its identity")
        created = timestamp(record.get("createdAt"))
        if "candidate" in record:
            saved = record["candidate"]
            if not isinstance(saved, dict) or any(
                saved.get(field) != record[field] for field in IDENTITY_FIELDS
            ):
                raise ValueError("Retained candidate differs from its catalogue identity")
            for field in OPTIONAL_IDENTITY_FIELDS:
                if field in saved:
                    require_digest(saved[field])
        if record.get("state") not in {"incomplete", "completed", "abandoned"}:
            raise ValueError("Invalid publication state")
        if record.get("delivery") not in {"pending", "complete"}:
            raise ValueError("Invalid evidence delivery state")
        if record.get("publicExposure") not in {"unknown", "public", "absent"}:
            raise ValueError("Invalid registry exposure state")
        if record["state"] == "completed":
            completion_predicate(record)
            if timestamp(record["completedAt"]) < created:
                raise ValueError("Completion precedes reservation")
            completed_by_profile.setdefault(record["profile"], []).append(record)
        elif record["delivery"] != "pending" or any(
            field in record for field in ("completedAt", "supersededAt", "evidenceIndexSha256")
        ):
            raise ValueError("Uncompleted release claims completed evidence")
        if record["state"] == "abandoned":
            if (
                timestamp(record.get("abandonedAt")) < created
                or not isinstance(record.get("reason"), str)
                or not record["reason"].strip()
            ):
                raise ValueError("Invalid abandonment record")
        if record["publicExposure"] == "absent":
            proof = record.get("absenceProof", {})
            if (
                record["state"] != "abandoned"
                or not isinstance(proof, dict)
                or proof.get("digest") != record["registryDigest"]
                or proof.get("digestMissing") is not True
                or proof.get("versionTagMissing") is not True
                or timestamp(proof.get("checkedAt")) < timestamp(record["abandonedAt"])
            ):
                raise ValueError("Only confirmed absence can retire an abandoned public candidate")
        for field in ("latestAttempt", "lastKnownVulnerable"):
            attempt = record.get(field)
            if attempt is None:
                continue
            if not isinstance(attempt, dict) or attempt.get("digest") != record["registryDigest"]:
                raise ValueError("Assessment belongs to another digest")
            timestamp(attempt.get("assessedAt"))
            outcomes = (
                {"vulnerable"}
                if field == "lastKnownVulnerable"
                else {"clean", "vulnerable", "unavailable", "running"}
            )
            if attempt.get("outcome") not in outcomes:
                raise ValueError("Invalid assessment outcome")
    if set(value["current"]) != set(completed_by_profile):
        raise ValueError("Current pointers do not cover completed profiles")
    for profile, records in completed_by_profile.items():
        ordered = sorted(records, key=lambda r: version(r["version"]))
        latest = ordered[-1]
        if value["current"][profile] != key(latest) or "supersededAt" in latest:
            raise ValueError("Current pointer does not identify the newest completed release")
        for previous, following in zip(ordered, ordered[1:], strict=False):
            if previous.get("supersededAt") != following["completedAt"] or timestamp(
                previous["completedAt"]
            ) > timestamp(following["completedAt"]):
                raise ValueError("Inconsistent release supersession history")


def validate_transition(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Prevent storage writes from deleting reservations or rewriting committed identities."""
    validate_catalogue(before)
    validate_catalogue(after)
    for name in after["releases"].keys() - before["releases"].keys():
        if after["releases"][name]["state"] != "incomplete":
            raise ValueError("New releases require a prior durable incomplete reservation")
    for name, old in before["releases"].items():
        new = after["releases"].get(name)
        if new is None or any(new.get(field) != old[field] for field in IDENTITY_FIELDS):
            raise ValueError("Release reservations and identities are permanent")
        permanent = ["createdAt"]
        if "candidate" in old:
            permanent.append("candidate")
        if old["state"] == "completed":
            permanent.extend(("state", "completedAt", "evidenceIndexSha256"))
        elif old["state"] == "abandoned":
            permanent.extend(("state", "abandonedAt", "reason"))
        if old["delivery"] == "complete":
            permanent.append("delivery")
        if "supersededAt" in old:
            permanent.append("supersededAt")
        if any(new.get(field) != old[field] for field in permanent):
            raise ValueError("Committed release history cannot be rewritten")
        if old.get("latestAttempt") and (
            not new.get("latestAttempt")
            or timestamp(new["latestAttempt"]["assessedAt"])
            < timestamp(old["latestAttempt"]["assessedAt"])
        ):
            raise ValueError("Assessment history cannot move backwards")
        if (
            old.get("latestAttempt")
            and new.get("latestAttempt")
            and timestamp(new["latestAttempt"]["assessedAt"])
            == timestamp(old["latestAttempt"]["assessedAt"])
            and new["latestAttempt"] != old["latestAttempt"]
        ):
            raise ValueError("Conflicting assessments have the same timestamp")
        if old.get("lastKnownVulnerable") and (
            not new.get("lastKnownVulnerable")
            or timestamp(new["lastKnownVulnerable"]["assessedAt"])
            < timestamp(old["lastKnownVulnerable"]["assessedAt"])
        ):
            raise ValueError("Known vulnerability history cannot be erased")


def reserve(catalogue: dict[str, Any], candidate: dict[str, Any], at: str) -> dict[str, Any]:
    """Reserve a version before registry writes; retries must retain every identity."""
    timestamp(at)
    result = copy.deepcopy(catalogue)
    name = key(candidate)
    retained = {field: candidate[field] for field in IDENTITY_FIELDS}
    retained.update(
        {field: candidate[field] for field in OPTIONAL_IDENTITY_FIELDS if field in candidate}
    )
    existing = result["releases"].get(name)
    if existing:
        if any(existing.get(field) != candidate.get(field) for field in IDENTITY_FIELDS):
            raise ValueError("A reserved version cannot change identity or owner")
        if existing.get("candidate", retained) != retained:
            raise ValueError("Reserved preparation evidence cannot change")
        if existing["state"] == "abandoned":
            raise ValueError("An abandoned version is permanently retired")
        if existing["state"] == "completed":
            return result
    current = result["current"].get(candidate["profile"])
    if current and version(candidate["version"]) <= version(result["releases"][current]["version"]):
        raise ValueError("Incomplete release was overtaken by a completed version")
    if (
        not current
        and not any(r["profile"] == candidate["profile"] for r in result["releases"].values())
        and candidate["version"] != "0.1.0"
    ):
        raise ValueError("The first reserved version must be 0.1.0")
    if not existing:
        result["releases"][name] = {field: candidate[field] for field in IDENTITY_FIELDS} | {
            "state": "incomplete",
            "createdAt": at,
            "publicExposure": "unknown",
            "delivery": "pending",
            "candidate": retained,
        }
    return result


def complete(
    catalogue: dict[str, Any], candidate: dict[str, Any], evidence_hash: str, at: str
) -> dict[str, Any]:
    """Commit completion and supersession atomically before authorizing a signature."""
    require_digest(evidence_hash)
    result = reserve(catalogue, candidate, at)
    name = key(candidate)
    record = result["releases"][name]
    if record["state"] == "completed":
        if record["evidenceIndexSha256"] != evidence_hash:
            raise ValueError("Completed evidence is immutable")
        return result
    if name not in catalogue["releases"]:
        raise ValueError("Completion requires a prior durable reservation")
    previous = result["current"].get(candidate["profile"])
    if previous:
        result["releases"][previous]["supersededAt"] = at
    record.update(
        state="completed",
        completedAt=at,
        evidenceIndexSha256=evidence_hash,
        publicExposure="public",
    )
    result["current"][candidate["profile"]] = name
    return result


def completion_predicate(record: dict[str, Any]) -> dict[str, Any]:
    """Derive the signed statement only from a committed completion record."""
    validate_identity(record)
    if record.get("state") != "completed":
        raise ValueError("Only committed completion authorizes signing")
    require_digest(record.get("evidenceIndexSha256"))
    timestamp(record.get("completedAt"))
    return {"schemaVersion": 1, "state": "completed"} | {
        field: record[field]
        for field in (
            "profile",
            "version",
            "sourceCommit",
            "sourceRef",
            "registryDigest",
            "assessedManifestDigest",
            "evidenceIndexSha256",
        )
    }


def abandon(catalogue: dict[str, Any], name: str, reason: str, at: str) -> dict[str, Any]:
    """Retire an incomplete version without assuming its public image disappeared."""
    timestamp(at)
    if not reason.strip():
        raise ValueError("Abandonment requires a reason")
    result = copy.deepcopy(catalogue)
    record = result["releases"][name]
    if record["state"] == "completed":
        raise ValueError("Completion is irreversible")
    if record["state"] != "abandoned":
        record.update(state="abandoned", abandonedAt=at, reason=reason)
    return result


def monitoring_end(record: dict[str, Any]) -> datetime | None:
    """Apply the 90-day window only to superseded completed releases."""
    if record["state"] == "completed" and record.get("supersededAt"):
        return timestamp(record["supersededAt"]) + timedelta(days=90)
    if record["state"] == "abandoned" and record.get("publicExposure") == "absent":
        proof = record.get("absenceProof", {})
        if (
            proof.get("digest") == record["registryDigest"]
            and proof.get("digestMissing") is True
            and proof.get("versionTagMissing") is True
        ):
            return timestamp(proof.get("checkedAt"))
    return None


def assessment_status(record: dict[str, Any], at: str) -> str:
    """Evaluate freshness on read, preserving failed attempts over older passing scans."""
    clock = timestamp(at)
    end = monitoring_end(record)
    if end is not None and clock >= end:
        return "no-longer-monitored"
    attempt = record.get("latestAttempt")
    if not isinstance(attempt, dict) or attempt.get("digest") != record["registryDigest"]:
        return "unavailable"
    if attempt.get("outcome") == "vulnerable":
        return "vulnerable"
    if attempt.get("outcome") != "clean":
        return "unavailable"
    age = clock - timestamp(attempt.get("assessedAt"))
    if age < timedelta(0):
        return "unavailable"
    return "stale" if age >= timedelta(hours=48) else "clean"


def observe(catalogue: dict[str, Any], name: str, assessment: dict[str, Any]) -> dict[str, Any]:
    """Append an assessment outcome without erasing previously known vulnerabilities."""
    at = timestamp(assessment.get("assessedAt"))
    if assessment.get("outcome") not in {"clean", "vulnerable", "unavailable", "running"}:
        raise ValueError("Unknown assessment outcome")
    result = copy.deepcopy(catalogue)
    record = result["releases"][name]
    if assessment.get("digest") != record["registryDigest"]:
        raise ValueError("Assessment belongs to another digest")
    previous = record.get("latestAttempt")
    if previous and at < timestamp(previous["assessedAt"]):
        raise ValueError("Assessment history cannot move backwards")
    record["latestAttempt"] = copy.deepcopy(assessment)
    if assessment["outcome"] == "vulnerable":
        record["lastKnownVulnerable"] = copy.deepcopy(assessment)
    return result


def scan_result(report: dict[str, Any], image_id: str, at: str) -> dict[str, Any]:
    """Require an identified Grype assessment with a valid database and no ignored matches."""
    require_digest(image_id)
    timestamp(at)
    source = report.get("source", {})
    if source.get("type") != "image" or source.get("target", {}).get("imageID") != image_id:
        raise ValueError("Vulnerability report identifies another image")
    descriptor = report.get("descriptor", {})
    database = descriptor.get("db", {})
    if (
        database.get("valid") is not True
        or bool(database.get("error"))
        or not database.get("built")
        or not database.get("schemaVersion")
        or not database.get("from")
    ):
        raise ValueError("Vulnerability database is unavailable or invalid")
    if timestamp(database["built"]) > timestamp(at):
        raise ValueError("Vulnerability database is dated in the future")
    matches = report.get("matches")
    if not isinstance(matches, list) or report.get("ignoredMatches", []) != []:
        raise ValueError("Incomplete or filtered vulnerability report")
    high = []
    for match in matches:
        vuln = match["vulnerability"]
        severity = vuln["severity"]
        if severity not in {"Negligible", "Low", "Medium", "High", "Critical", "Unknown"}:
            raise ValueError("Unknown vulnerability severity")
        if severity in {"High", "Critical"}:
            high.append({"id": vuln["id"], "severity": severity})
    return {
        "assessedAt": at,
        "outcome": "vulnerable" if high else "clean",
        "database": database,
        "findings": high,
    }
