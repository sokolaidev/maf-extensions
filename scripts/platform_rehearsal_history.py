"""Replay pinned monitor findings while acquiring current delivery authority separately."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import vendor_fix_evidence
from container_release import digest, read, require_digest, write
from container_release_history import GitHub
from image_release_needed import git
from image_remediation_evidence import acquire_with_retry, current, monitor_source, restore_monitor
from image_scan_policy import BICEP_ROUTES
from plan_platform_remediation import plan, proposal

MANIFEST = "scripts/platform_rehearsal_cases.json"
CASE = "historical-rebuild"
APK_CASE = "historical-apk-rebuild"
CASES = {
    CASE: (sorted(BICEP_ROUTES), ["bicep-docker-sbx-0.2.2"]),
    APK_CASE: (["graphviz"], ["graphviz-0.2.6"]),
}


def fixture(root: Path, case: str = CASE) -> dict[str, Any]:
    """Only the committed case manifest selects historical inputs; dispatch supplies no URLs."""
    if case not in CASES:
        raise ValueError("Unknown historical rebuild case")
    return read(root / MANIFEST)["cases"][case]["fixture"]


def verify_files(root: Path, directory: Path, case: str = CASE) -> list[Path]:
    """Pin the index and every consumed member independently of downloaded artifact metadata."""
    expected = fixture(root, case)
    targets = expected["directories"]
    if targets != CASES[case][1]:
        raise ValueError("Unsupported historical rebuild fixture scope")
    names = {"monitor-index.json"} | {
        f"{target}/{name}"
        for target in targets
        for name in ("observation.json", "assessment.json", "sbom.syft.json", "grype.json")
    }
    if set(expected["files"]) != names:
        raise ValueError("Historical fixture must pin every consumed evidence file")
    for name, value in expected["files"].items():
        path = directory / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(directory.resolve())
            or digest(path) != require_digest(value)
        ):
            raise ValueError("Historical fixture evidence differs from the reviewed hashes")
    return [directory / name for name in targets]


def receipt(root: Path, source: str, case: str = CASE) -> dict[str, Any]:
    """Describe historical finding provenance without relabelling its assessment timestamps."""
    return {"source": source, "case": case, "rehearsalFixture": fixture(root, case)}


def authority(work: Path, case: str = CASE) -> dict[str, Any]:
    """Scope the acquired current snapshot to the profiles this replay actually rebuilds."""
    retained = read(work / "current" / "receipt.json")
    images = retained.get("images")
    profiles = CASES[case][0]
    if not isinstance(images, dict) or not set(profiles) <= images.keys():
        raise ValueError("Current authority does not cover every historical rebuild profile")
    return {**retained, "images": {name: images[name] for name in profiles}}


def validate(
    root: Path, work: Path, *, case: str = CASE
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Recompute the replay proposal and require current authority for every inherited profile."""
    source = git(root, "rev-parse", "HEAD").strip()
    current_receipt = authority(work, case)
    if current_receipt["source"] != source or read(work / "receipt.json") != receipt(
        root, source, case
    ):
        raise ValueError("Historical rehearsal source or fixture changed")
    vendor = vendor_fix_evidence.load(work / "vendor-security") if case == APK_CASE else None
    expected = plan(root, verify_files(root, work / "evidence", case), vendor=vendor)
    if expected != read(work / "plan.json"):
        raise ValueError("Historical plan differs from pinned evidence and current source")
    if expected["affectedProfiles"] != CASES[case][0] or expected["updates"]:
        raise ValueError("Historical rebuild requires its exact profiles and no source edits")
    return expected, proposal(root, expected)


def acquire(root: Path, work: Path, case: str = CASE) -> dict[str, Any]:
    """Restore immutable historical findings and retain fresh production delivery authority."""
    work.mkdir(parents=True, exist_ok=False)
    expected = fixture(root, case)
    client = GitHub()
    producer = monitor_source(client, expected["monitor"])
    if producer != expected["producer"]:
        raise ValueError("Historical monitor producer differs from the reviewed fixture")
    restore_monitor(
        client, expected["monitor"], producer, set(expected["directories"]), work / "evidence"
    )
    directories = verify_files(root, work / "evidence", case)
    vendor = vendor_fix_evidence.acquire(work / "vendor-security") if case == APK_CASE else None
    replay = plan(root, directories, vendor=vendor)

    def authority_scope(_root: Path, _directories: list[Path]) -> dict[str, Any]:
        return {"kind": "platform-rehearsal-authority", "affectedProfiles": CASES[case][0]}

    acquired = acquire_with_retry(
        client,
        root,
        work / "current",
        selected_profiles=CASES[case][0],
        planner=authority_scope,
        require_all_profiles=False,
    )
    if acquired is None:
        raise ValueError("Rehearsal acquisition deferred; retry after evidence stabilizes")
    current_receipt = authority(work, case)
    write(work / "receipt.json", receipt(root, current_receipt["source"], case))
    write(work / "plan.json", replay)
    validate(root, work, case=case)
    current(client, current_receipt)
    return replay
