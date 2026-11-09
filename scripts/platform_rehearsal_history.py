"""Replay pinned monitor findings while acquiring current delivery authority separately."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from container_release import digest, read, require_digest, write
from container_release_history import GitHub
from image_release_needed import git
from image_remediation_evidence import acquire_with_retry, current, monitor_source, restore_monitor
from image_scan_policy import BICEP_ROUTES
from plan_platform_remediation import plan, proposal

MANIFEST = "scripts/platform_rehearsal_cases.json"
CASE = "historical-rebuild"


def fixture(root: Path) -> dict[str, Any]:
    """Only the committed case manifest selects historical inputs; dispatch supplies no URLs."""
    return read(root / MANIFEST)["cases"][CASE]["fixture"]


def verify_files(root: Path, directory: Path) -> list[Path]:
    """Pin the index and every consumed member independently of downloaded artifact metadata."""
    expected = fixture(root)
    targets = expected["directories"]
    if targets != ["bicep-docker-sbx-0.2.2"]:
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


def receipt(root: Path, source: str) -> dict[str, Any]:
    """Describe historical finding provenance without relabelling its assessment timestamps."""
    return {"source": source, "rehearsalFixture": fixture(root)}


def authority(work: Path) -> dict[str, Any]:
    """Scope the acquired current snapshot to the profiles this replay actually rebuilds."""
    retained = read(work / "current" / "receipt.json")
    images = retained.get("images")
    if not isinstance(images, dict) or not set(BICEP_ROUTES) <= images.keys():
        raise ValueError("Current authority does not cover every historical rebuild profile")
    return {**retained, "images": {name: images[name] for name in sorted(BICEP_ROUTES)}}


def validate(root: Path, work: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Recompute the replay proposal and require current authority for every inherited profile."""
    source = git(root, "rev-parse", "HEAD").strip()
    current_receipt = authority(work)
    if current_receipt["source"] != source or read(work / "receipt.json") != receipt(root, source):
        raise ValueError("Historical rehearsal source or fixture changed")
    expected = plan(root, verify_files(root, work / "evidence"))
    if expected != read(work / "plan.json"):
        raise ValueError("Historical plan differs from pinned evidence and current source")
    if expected["affectedProfiles"] != sorted(BICEP_ROUTES) or expected["updates"]:
        raise ValueError("Historical rebuild requires all Bicep profiles and no source edits")
    return expected, proposal(root, expected)


def acquire(root: Path, work: Path) -> dict[str, Any]:
    """Restore immutable historical findings and retain fresh production delivery authority."""
    work.mkdir(parents=True, exist_ok=False)
    expected = fixture(root)
    client = GitHub()
    producer = monitor_source(client, expected["monitor"])
    if producer != expected["producer"]:
        raise ValueError("Historical monitor producer differs from the reviewed fixture")
    restore_monitor(
        client, expected["monitor"], producer, set(expected["directories"]), work / "evidence"
    )
    directories = verify_files(root, work / "evidence")
    replay = plan(root, directories)
    acquired = acquire_with_retry(
        client,
        root,
        work / "current",
        selected_profiles=BICEP_ROUTES,
        planner=plan,
        require_all_profiles=False,
    )
    if acquired is None:
        raise ValueError("Rehearsal acquisition deferred; retry after evidence stabilizes")
    current_receipt = authority(work)
    write(work / "receipt.json", receipt(root, current_receipt["source"]))
    write(work / "plan.json", replay)
    validate(root, work)
    current(client, current_receipt)
    return replay
