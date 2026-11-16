"""Exercise real package diffs on reviewed fixture inputs with separate current authority."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import vendor_fix_evidence
from container_release import digest, read, require_digest, write
from container_release_history import GitHub
from image_release_needed import git
from image_remediation_evidence import acquire_with_retry, current, monitor_source, restore_monitor
from plan_platform_remediation import NUGET, plan, proposal

MANIFEST = "scripts/platform_rehearsal_cases.json"
PREFIX = "tests/fixtures/image_dependencies/platform-positive/"
CASES = {
    "replay-bicep-update": {NUGET: PREFIX + "dependencies.csproj"},
    "replay-apk-pinned": {"images/graphviz-sandbox/Dockerfile": PREFIX + "Dockerfile"},
}

SUPPORT = {
    "replay-bicep-update": {"images/bicep-sandbox/source.Dockerfile": PREFIX + "source.Dockerfile"}
}


def overlays(case: str) -> dict[str, str]:
    """Separate build support fixtures from the package files permitted in a draft."""
    return CASES[case] | SUPPORT.get(case, {})


def rule(root: Path, case: str) -> dict[str, Any]:
    """Require an explicit reviewed case, never caller-supplied paths or URLs."""
    if case not in CASES:
        raise ValueError("Unknown positive replay case")
    return read(root / MANIFEST)["cases"][case]


def fixture_hashes(root: Path, case: str) -> dict[str, str]:
    """Bind the committed fixture bytes independently of native build inputs."""
    result = {}
    for name in overlays(case).values():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Replay input escapes the checkout")
        result[name] = digest(path)
    return result


@contextmanager
def overlay(root: Path, case: str) -> Iterator[None]:
    """Restore production bytes even when a fixture build or source gate fails."""
    fixture_hashes(root, case)
    originals = {}
    for name in overlays(case):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Replay destination escapes the checkout")
        originals[name] = path.read_bytes()
    try:
        for name, fixture in overlays(case).items():
            (root / name).write_bytes((root / fixture).read_bytes())
        yield
    finally:
        for name, raw in originals.items():
            (root / name).write_bytes(raw)


def directories(root: Path, work: Path, case: str) -> list[Path]:
    """Verify every consumed historical member against its reviewed immutable hash."""
    fixture = rule(root, case)["fixture"]
    targets = fixture["directories"]
    allowed = {
        "replay-bicep-update": ["bicep-docker-sbx-0.2.2"],
        "replay-apk-pinned": ["graphviz-0.2.6"],
    }
    if targets != allowed[case]:
        raise ValueError("Unexpected positive replay evidence scope")
    names = {"monitor-index.json"} | {
        f"{target}/{name}"
        for target in targets
        for name in ("observation.json", "assessment.json", "sbom.syft.json", "grype.json")
    }
    if set(fixture["files"]) != names:
        raise ValueError("Replay must pin every consumed historical member")
    evidence = work / "evidence"
    for name, value in fixture["files"].items():
        path = evidence / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(evidence.resolve())
            or digest(path) != require_digest(value)
        ):
            raise ValueError("Positive replay evidence differs from reviewed hashes")
    return [evidence / target for target in targets]


def receipt(root: Path, case: str, source: str) -> dict[str, Any]:
    """Identify the controlled baseline without changing historical observation times."""
    return {
        "source": source,
        "rehearsalFixture": rule(root, case)["fixture"],
        "case": case,
        "fixtureInputs": fixture_hashes(root, case),
    }


def authority(work: Path) -> dict[str, Any]:
    """Only freshly acquired production evidence authorizes App writes."""
    return read(work / "current" / "receipt.json")


def validate(root: Path, work: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Recompute the native dependency diff while the reviewed baseline overlay is active."""
    retained = read(work / "receipt.json")
    case = retained["case"]
    source = git(root, "rev-parse", "HEAD").strip()
    if retained != receipt(root, case, source) or authority(work)["source"] != source:
        raise ValueError("Positive replay source or fixture changed")
    profiles = rule(root, case)["profiles"]
    if set(authority(work).get("images", {})) != set(profiles):
        raise ValueError("Current authority differs from the positive replay scope")
    vendor = (
        vendor_fix_evidence.load(work / "vendor-security") if case == "replay-apk-pinned" else None
    )
    expected = plan(root, directories(root, work, case), vendor=vendor)
    if expected != read(work / "plan.json") or expected["affectedProfiles"] != profiles:
        raise ValueError("Positive replay plan differs from reviewed inputs")
    files = proposal(root, expected)
    if not files or not set(files) <= CASES[case].keys():
        raise ValueError("Positive replay requires its bounded nonempty dependency diff")
    return expected, files


def acquire(root: Path, work: Path, case: str) -> dict[str, Any]:
    """Authenticate historical findings and acquire independent current write authority."""
    work.mkdir(parents=True, exist_ok=False)
    selected = rule(root, case)
    fixture = selected["fixture"]
    client = GitHub()
    producer = monitor_source(client, fixture["monitor"])
    if producer != fixture["producer"]:
        raise ValueError("Positive replay monitor has another producer")
    restore_monitor(
        client, fixture["monitor"], producer, set(fixture["directories"]), work / "evidence"
    )
    vendor = (
        vendor_fix_evidence.acquire(work / "vendor-security")
        if case == "replay-apk-pinned"
        else None
    )
    expected = plan(root, directories(root, work, case), vendor=vendor)

    def scope(_root: Path, _directories: list[Path]) -> dict[str, Any]:
        return {"kind": "platform-rehearsal-authority", "affectedProfiles": selected["profiles"]}

    acquired = acquire_with_retry(
        client,
        root,
        work / "current",
        selected_profiles=selected["profiles"],
        planner=scope,
        require_all_profiles=False,
    )
    if acquired is None:
        raise ValueError("Positive replay acquisition deferred; retry after evidence stabilizes")
    write(work / "receipt.json", receipt(root, case, authority(work)["source"]))
    write(work / "plan.json", expected)
    validate(root, work)
    current(client, authority(work))
    return expected


def delivery_files(case: str, files: dict[str, bytes]) -> dict[str, bytes]:
    """Deliver the exact tested bytes to fixture paths, never to production recipes."""
    if not files or not set(files) <= CASES[case].keys():
        raise ValueError("Unplanned positive replay delivery path")
    return {CASES[case][name]: raw for name, raw in files.items()}
