"""Discover, qualify and propose stable Chrome image updates without publishing images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import chrome_inventory
import chrome_repository as repository
from check_container_release_image import check
from container_release import REPOSITORY, ROOT, now, read, scan_result, timestamp, write
from container_release_history import GitHub, positive_id
from image_release_needed import git
from image_remediation import outputs
from image_remediation_evidence import EvidenceChanging, deferred, main_head
from image_remediation_pr import publish
from image_security_evidence import record, verify_inventory
from platform_remediation import PlatformGitHub, competing_pulls
from prepare_container_release import assess

RECIPE = "images/document-export/Dockerfile"
PIN = re.compile(
    r"^ADD --checksum=sha256:(?P<sha256>[0-9a-f]{64}) "
    + re.escape(repository.BASE)
    + r"pool/main/g/google-chrome-stable/google-chrome-stable_"
    + r"(?P<version>[^ /]+)_amd64\.deb /tmp/chrome\.deb$",
    re.MULTILINE,
)


class ChromeGitHub(PlatformGitHub):
    """Use the shared App ownership protocol on a separate Chrome-only branch."""

    branch = "bot/chrome-image-update"
    title = "build: update document-export Chrome from verified stable packages"

    def __init__(self) -> None:
        super().__init__({RECIPE})


def proposal(original: bytes, selected: dict[str, Any]) -> tuple[str, bytes]:
    """Replace only the unique pinned Chrome URL and checksum, never other build inputs."""
    text = original.decode("utf-8")
    pins = list(PIN.finditer(text))
    if len(pins) != 1:
        raise ValueError("Expected one pinned Chrome archive")
    pin = pins[0]
    before = pin["version"]
    if repository.version(str(selected["version"])) < repository.version(before):
        raise ValueError("Chrome repository would downgrade the source pin")
    if selected["version"] == before:
        if selected["sha256"] != pin["sha256"]:
            raise ValueError("Chrome archive changed without a version change")
        return before, original
    replacement = f"ADD --checksum=sha256:{selected['sha256']} {selected['url']} /tmp/chrome.deb"
    return before, (text[: pin.start()] + replacement + text[pin.end() :]).encode()


def current(client: GitHub, receipt: dict[str, Any]) -> None:
    """Require a fresh discovery and unchanged main before every delivery boundary."""
    if (
        not timedelta(0)
        <= timestamp(now()) - timestamp(receipt["acquiredAt"])
        < timedelta(hours=24)
    ):
        raise ValueError("Chrome discovery receipt expired or is future dated")
    if main_head(client) != receipt["source"]:
        raise EvidenceChanging("Main changed during Chrome qualification")
    if conflicts := competing_pulls(client, {RECIPE}):
        raise EvidenceChanging(f"Competing source PRs: {conflicts}")


def validate(root: Path, work: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Reconstruct the sole edit from authenticated metadata and the committed source."""
    receipt = read(work / "receipt.json")
    if git(root, "rev-parse", "HEAD").strip() != receipt["source"]:
        raise ValueError("Chrome qualification source changed")
    selected = repository.metadata(work / "upstream")
    if selected != receipt["package"]:
        raise ValueError("Chrome discovery differs from retained metadata")
    repository.verify_bytes(
        work / "upstream/chrome.deb", int(selected["size"]), str(selected["sha256"])
    )
    original = subprocess.check_output(["git", "show", f"HEAD:{RECIPE}"], cwd=root, timeout=30)
    before, updated = proposal(original, selected)
    if updated == original or before != receipt["before"]:
        raise ValueError("Chrome proposal has no version update")
    changed = set(git(root, "diff", "HEAD", "--name-only").splitlines())
    if changed - {RECIPE} or (root / RECIPE).read_bytes() not in (original, updated):
        raise ValueError("Checkout differs from the qualified Chrome proposal")
    return receipt, {RECIPE: updated}


def acquire(root: Path, work: Path, client: GitHub) -> dict[str, Any]:
    """Discover newer stable packages independently of published-image vulnerability findings."""
    work.mkdir(parents=True, exist_ok=False)
    source = git(root, "rev-parse", "HEAD").strip()
    receipt: dict[str, Any] = {"schemaVersion": 1, "source": source, "acquiredAt": now()}
    current(client, receipt)
    selected = repository.acquire(work / "upstream")
    before, updated = proposal((root / RECIPE).read_bytes(), selected)
    receipt.update(package=selected, before=before)
    write(work / "receipt.json", receipt)
    changed = updated != (root / RECIPE).read_bytes()
    result = {
        "status": "update" if changed else "unchanged",
        "before": before,
        "after": selected["version"],
    }
    write(work / "discovery.json", result)
    outputs({"updates": str(changed).lower()})
    print(json.dumps(result), flush=True)
    return result


def qualify(root: Path, work: Path) -> dict[str, Any]:
    """Recheck the exact browser, image and unfiltered assessment before admitting delivery."""
    receipt, files = validate(root, work)
    directory = work / "candidate"
    built, runtime = read(directory / "build.json"), read(directory / "runtime.json")
    image = built["local_image_id"]
    if (built["source_commit"], built["profile"], built["platform"]) != (
        receipt["source"],
        "document-export",
        "linux/amd64",
    ):
        raise ValueError("Chrome build identifies another source or profile")
    if (
        runtime.get("imageId"),
        runtime.get("profile"),
        runtime.get("passed"),
        runtime.get("containersRemaining"),
    ) != (image, "document-export", True, []):
        raise ValueError("Chrome runtime qualification is incomplete")
    sbom = read(directory / "sbom.syft.json")
    verify_inventory(sbom, image)
    chrome = chrome_inventory.component(sbom)
    if chrome is None or chrome["version"] != receipt["package"]["version"].rsplit("-", 1)[0]:
        raise ValueError("Candidate inventory does not contain the proposed Chrome version")
    packages = [a for a in sbom["artifacts"] if a.get("name") == repository.PACKAGE]
    if len(packages) != 1 or packages[0]["version"] != receipt["package"]["version"]:
        raise ValueError("Candidate Chrome Debian revision differs from the proposal")
    assessment = read(directory / "assessment.json")
    at = assessment["assessedAt"]
    if not timedelta(0) <= timestamp(now()) - timestamp(at) < timedelta(hours=24):
        raise ValueError("Chrome candidate assessment expired")
    if (
        not timedelta(0)
        <= timestamp(now()) - timestamp(assessment["database"]["built"])
        < timedelta(hours=48)
    ):
        raise ValueError("Chrome vulnerability database expired")
    if (
        scan_result(read(directory / "grype.json"), image, at, sbom=sbom) != assessment
        or assessment["outcome"] != "clean"
    ):
        raise ValueError("Chrome candidate did not pass the High/Critical security gate")
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    if read(directory / "inputs.json") != hashes:
        raise ValueError("Chrome candidate build inputs differ from the proposal")
    return {
        "status": "ready",
        "kind": "chrome",
        "source": receipt["source"],
        "files": hashes,
        "before": receipt["before"],
        "after": receipt["package"]["version"],
        "scans": [
            {
                "variant": "candidate",
                "profile": "document-export",
                "imageId": image,
                "assessment": assessment,
            }
        ],
    }


def build(root: Path, work: Path) -> dict[str, Any]:
    """Build the ordinary recipe and run the existing offline consumer and scan checks."""
    receipt, files = validate(root, work)
    directory = work / "candidate"
    directory.mkdir(exist_ok=False)
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    subprocess.run(
        [
            "docker",
            "build",
            "--pull",
            "--platform",
            "linux/amd64",
            "--tag",
            "maf-chrome-update:candidate",
            "images/document-export",
        ],
        cwd=root,
        check=True,
        timeout=1800,
    )
    details = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", "maf-chrome-update:candidate"], timeout=60
        )
    )
    built = record(details, receipt["source"], "document-export")
    write(directory / "build.json", built)
    write(
        directory / "inputs.json",
        {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    )
    image = built["local_image_id"]
    write(directory / "runtime.json", check("document-export", image, directory))
    assess(directory, image)
    report = qualify(root, work)
    write(work / "qualified.json", report)
    return report


def main() -> None:
    """Keep authenticated discovery, candidate qualification and App delivery explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("acquire", "build", "publish"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--bot")
    parser.add_argument("--artifact-id", type=int)
    args = parser.parse_args()
    client = ChromeGitHub()
    try:
        if args.command == "acquire":
            acquire(ROOT, args.work, client)
        elif args.command == "build":
            build(ROOT, args.work)
        else:
            if not args.bot or not args.artifact_id:
                raise ValueError("Chrome delivery requires a bot and retained artifact")
            report = qualify(ROOT, args.work)
            if report != read(args.work / "qualified.json"):
                raise ValueError("Chrome qualified report changed")
            receipt, files = validate(ROOT, args.work)
            result = publish(
                client,
                receipt,
                report,
                files,
                args.bot,
                f"https://github.com/{REPOSITORY}/actions/runs/"
                f"{positive_id(int(os.environ['GITHUB_RUN_ID']))}/artifacts/{positive_id(args.artifact_id)}",
                freshness=current,
            )
            write(args.work / "publication.json", result)
            print(json.dumps(result), flush=True)
    except EvidenceChanging as error:
        deferred(args.work, error, args.command)
        outputs({"updates": "false"})


if __name__ == "__main__":
    main()
