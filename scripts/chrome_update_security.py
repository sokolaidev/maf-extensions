"""Bind automatic Chrome proposals to current published-image security findings."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import chrome_inventory
import chrome_repository
import image_remediation_evidence as monitoring
from container_release import read
from container_release_history import GitHub
from plan_go_remediation import evidence

PROFILE = "document-export"


def browser_version(value: str) -> tuple[int, ...]:
    """Compare scanner browser versions without inventing a Debian package revision."""
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", value) is None:
        raise ValueError("Unsupported Chrome fixed version")
    return tuple(map(int, value.split(".")))


def plan(directories: list[Path], before: str, after: str) -> dict[str, Any]:
    """Select declared fixes that require advancing the current source pin."""
    if len(directories) != 1:
        raise ValueError("Chrome security evidence must identify one current image")
    directory = directories[0]
    identity, matches, hashes = evidence(directory)
    if identity["profile"] != PROFILE:
        raise ValueError("Chrome security evidence identifies another profile")
    browser = chrome_inventory.component(read(directory / "sbom.syft.json"))
    if browser is None:
        raise ValueError("Published image has no Chrome inventory")
    old = chrome_repository.version(before)[:4]
    target = chrome_repository.version(after)[:4]
    findings = []
    for match in matches:
        artifact, vulnerability = match["artifact"], match["vulnerability"]
        if artifact["id"] != browser["id"]:
            continue
        fix = vulnerability.get("fix", {})
        if fix.get("state") != "fixed":
            continue
        versions = fix.get("versions", [])
        if not versions or any(
            not isinstance(v, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", v)
            for v in versions
        ):
            continue
        fixed = min(versions, key=browser_version)
        if not (
            browser_version(browser["version"]) < browser_version(fixed)
            and old < browser_version(fixed) <= target
        ):
            continue
        if re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", vulnerability["id"]) is None:
            raise ValueError("Invalid Chrome vulnerability identity")
        findings.append(
            {
                "id": vulnerability["id"],
                "severity": vulnerability["severity"],
                "installed": browser["version"],
                "fixedVersion": fixed,
            }
        )
    return {
        "schemaVersion": 1,
        "affectedProfiles": [PROFILE] if findings else [],
        "before": before,
        "after": after,
        "evidence": {**identity, "files": hashes},
        "findings": sorted(findings, key=lambda row: (row["id"], row["fixedVersion"])),
    }


def acquire(client: GitHub, root: Path, directory: Path, before: str, after: str) -> dict[str, Any]:
    """Reuse authenticated release and monitor acquisition, scoped to document-export."""
    result = monitoring.acquire_with_retry(
        client,
        root,
        directory,
        selected_profiles=[PROFILE],
        planner=lambda _root, paths: plan(paths, before, after),
    )
    if result is None:
        raise monitoring.EvidenceChanging("Current Chrome monitoring or catalogue is changing")
    return result


def validate(directory: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Recompute security eligibility from the monitor bytes bound to the current catalogue."""
    retained = read(directory / "receipt.json")
    if retained != receipt.get("security") or retained["source"] != receipt["source"]:
        raise ValueError("Chrome security receipt differs from discovery")
    monitoring.require_image_scope(retained, [PROFILE])
    record = retained["images"][PROFILE]["record"]
    name = f"{PROFILE}-{record['version']}"
    if re.fullmatch(r"document-export-[0-9]+\.[0-9]+\.[0-9]+", name) is None or retained[
        "directories"
    ] != [name]:
        raise ValueError("Chrome security receipt identifies another evidence directory")
    path = directory / "evidence" / name
    observed = read(path / "observation.json")
    if observed.get("candidate") != record["candidate"] or observed.get("assessment") != {
        key: value for key, value in record["latestAttempt"].items() if key != "evidenceRelease"
    }:
        raise ValueError("Chrome security evidence differs from the current catalogue")
    expected = plan([path], receipt["before"], receipt["package"]["version"])
    if expected != read(directory / "plan.json"):
        raise ValueError("Chrome security plan differs from retained monitoring")
    return expected


def requirement(expected: dict[str, Any]) -> dict[str, str] | None:
    """Describe a verified security update without treating newer versions as requirements."""
    if not expected["findings"]:
        return None
    identity = expected["evidence"]
    advisories = ", ".join(sorted({row["id"] for row in expected["findings"]}))
    return {
        "kind": "security",
        "reason": (
            f"Current published {PROFILE} {identity['version']} ({identity['registryDigest']}) "
            f"has applicable High/Critical Chrome findings: {advisories}. "
            f"Chrome {expected['after']} reaches their declared fixed versions."
        ),
    }
