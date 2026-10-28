"""Require completed, currently clean matching images before releasing image-bound kinds."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from container_release import PREFIX, REPOSITORY, ROOT, read
from container_release_history import GitHub, History
from container_release_status import report
from image_release_needed import baseline, changed_inputs
from select_image_security import metadata_only

KIND_IMAGES = {
    "maf-sandbox-document": ("document",),
    "maf-sandbox-bicep": ("bicep", "bicep-avm"),
    "maf-sandbox-drawio": ("drawio", "drawio-export"),
    "maf-sandbox-shell": ("shell",),
    "maf-sandbox-terraform": ("terraform-random", "opentofu-random", "terraform-avm"),
}


def git(root: Path, *args: str) -> str:
    """Read committed release inputs rather than local build artifacts."""
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, encoding="utf-8", stderr=subprocess.PIPE
    ).strip()


def revision(root: Path, value: str) -> str:
    """Resolve a revision without interpreting it as a Git option."""
    return git(root, "rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}")


def document(root: Path, ref: str, path: str) -> dict[str, Any]:
    """Read package metadata from the commit being assessed."""
    return tomllib.loads(git(root, "show", f"{ref}:{path}"))


def releasing(root: Path, base: str, head: str) -> list[str]:
    """Select real kind version changes, independent of PR title, author or changed-path filters."""
    before = set(git(root, "ls-tree", "-r", "--name-only", base, "packages").splitlines())
    after = set(git(root, "ls-tree", "-r", "--name-only", head, "packages").splitlines())
    result = []
    for package in KIND_IMAGES:
        path = f"packages/{package}/pyproject.toml"
        if path not in after:
            if path in before:
                raise ValueError(f"{package}: release metadata was removed")
            continue
        current = document(root, head, path)["project"]
        if current["name"] != package:
            raise ValueError(f"{package}: distribution identity changed")
        if path not in before and current["version"] == "0.0.0":
            continue
        if (
            path not in before
            or document(root, base, path)["project"]["version"] != current["version"]
        ):
            result.append(package)
    return result


def matching_source(root: Path, source: str, head: str, package: str, profile: str) -> None:
    """Require qualified SDK and image inputs, allowing version-only metadata bumps."""
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Image qualification has no immutable source commit")
    packages = (package, "maf-sandbox", "maf-sandbox-docker")
    for name in packages:
        path = f"packages/{name}/src"
        if git(root, "rev-parse", f"{source}:{path}") != git(root, "rev-parse", f"{head}:{path}"):
            raise ValueError(f"{package}: image qualification predates changed {name} source")
    paths = git(root, "diff", "--name-only", "--no-renames", source, head, "--").splitlines()
    ignored = metadata_only(root, source, head, paths)
    inputs = {"uv.lock", "pyproject.toml", "uv.toml", ".python-version"}
    inputs.update(f"packages/{name}/pyproject.toml" for name in packages)
    changed = inputs.intersection(paths).difference(ignored)
    if changed:
        raise ValueError(
            f"{package}: image qualification has different dependency/build inputs: "
            + ", ".join(sorted(changed))
        )

    image_inputs = changed_inputs(root, source, head, profile)
    if image_inputs:
        raise ValueError(
            f"{package}: image qualification predates changed {profile} image inputs: "
            + ", ".join(image_inputs)
        )


def qualified(directory: Path, candidate: dict[str, Any]) -> None:
    """Require indexed anonymous-pull and runtime evidence for the authenticated image."""
    index = read(directory / "evidence-index.json")
    if "qualification.json" not in index:
        raise ValueError("Signed evidence does not include image qualification")
    value = read(directory / "qualification.json")
    runtime = value.get("runtime", {})
    if (
        value.get("candidate") != candidate
        or value.get("runId") != candidate["attemptId"]
        or value.get("anonymousPullVerified") is not True
        or value.get("identity", {}).get("candidateIdentityVerified") is not True
        or runtime.get("passed") is not True
        or runtime.get("profile") != candidate["profile"]
        or runtime.get("imageId") != candidate["imageId"]
        or runtime.get("containersRemaining") != []
    ):
        raise ValueError("Published image lacks matching successful consumer qualification")


def refusal(package: str, profile: str, digest: str, status: dict[str, Any]) -> str:
    """Explain the failed readiness conditions without exposing raw scanner or subprocess output."""
    monitoring = status.get("monitoringStatus", "unavailable")
    lines = [
        f"{package}/{profile}: current image assessment is {monitoring}; release refused",
        f"Image: {PREFIX}/{profile}@{digest}",
    ]
    if status.get("reason"):
        lines.append(f"Reason: {status['reason']}")
    requirements = (
        ("publicationState", "completed"),
        ("releaseScope", "current"),
        ("digest", digest),
        ("publicationFailure", None),
    )
    mismatches = [
        f"{field}: {status[field]} (required: {expected})"
        for field, expected in requirements
        if field in status and status[field] != expected
    ]
    lines.extend(mismatches)
    monitor = status.get("observedMonitorWorkflow") or status.get("monitorWorkflow") or {}
    if monitor:
        lines.append(
            f"Latest observed monitor: https://github.com/{REPOSITORY}/actions/runs/"
            f"{monitor['id']}/attempts/{monitor['run_attempt']} "
            f"(status={monitor.get('status')}, conclusion={monitor.get('conclusion')})"
        )
    attempt = status.get("latestAttempt") or {}
    if attempt:
        lines.append(
            f"Recorded image assessment: {attempt.get('outcome', 'unknown')} "
            f"(run={attempt.get('monitorRunId')}, attempt={attempt.get('monitorRunAttempt')}, "
            f"assessedAt={attempt.get('assessedAt', 'unknown')})"
        )
        if attempt.get("reason"):
            lines.append(f"Image assessment reason: {attempt['reason']}")
    discovery = status.get("registryDiscovery") or {}
    if discovery.get("outcome") == "unavailable":
        lines.append("Registry discovery is unavailable; current registry contents are unverified.")
    if mismatches:
        action = "Inspect the catalogue and restore a completed, matching current image release."
    elif monitoring == "vulnerable":
        action = "Remediate the image findings, then publish and qualify a replacement image."
    elif (
        monitoring == "unavailable"
        and monitor.get("status") in {"queued", "pending", "requested", "waiting", "in_progress"}
        and status.get("reason")
        in {
            "Catalogue does not include the latest monitor workflow attempt",
            "Latest monitor workflow has no confirmed successful state delivery",
            "Monitor attempt changed during retrieval",
        }
    ):
        action = (
            "Wait for the latest monitor to finish and record a clean assessment for this image, "
            "then rerun the blocked workflow. An earlier clean assessment does not satisfy "
            "the latest-monitor requirement."
        )
    elif monitoring == "stale":
        action = (
            "Run the container image monitor and obtain a fresh clean assessment before retrying."
        )
    elif monitoring == "no-longer-monitored":
        action = "Publish and qualify a current image that is still monitored before retrying."
    else:
        action = (
            "Inspect the monitor and catalogue evidence; restore a current "
            "clean assessment before retrying."
        )
    lines.append(f"Next step: {action}")
    return "\n".join(lines)


def check(root: Path, head: str, packages: list[str]) -> list[dict[str, Any]]:
    """Fail closed on missing, incompatible, vulnerable, stale or unavailable release evidence."""
    selected = [package for package in packages if package in KIND_IMAGES]
    if not selected:
        return []
    client = GitHub()
    snapshot = History(client).head()
    if snapshot is None:
        raise ValueError("No completed image catalogue is available")
    results = []
    for package in selected:
        for profile in KIND_IMAGES[package]:
            name = snapshot.catalogue["current"].get(profile)
            if name is None:
                raise ValueError(
                    f"{package}: no completed {profile} image; publish and qualify it first"
                )
            record = snapshot.catalogue["releases"][name]
            if record["profile"] != profile:
                raise ValueError("Current catalogue entry identifies another profile")
            matching_source(root, record["sourceCommit"], head, package, profile)
            with tempfile.TemporaryDirectory(prefix="kind-release-image-") as temporary:
                directory = Path(temporary)
                evidence = baseline(client, record, directory)
                qualified(directory, record["candidate"])
            status = report(record["candidate"], github=client)
            if (
                status.get("monitoringStatus") != "clean"
                or status.get("publicationState") != "completed"
                or status.get("releaseScope") != "current"
                or status.get("digest") != record["registryDigest"]
                or status.get("publicationFailure") is not None
            ):
                raise ValueError(refusal(package, profile, record["registryDigest"], status))
            results.append(
                {
                    "package": package,
                    "profile": profile,
                    "image": f"{PREFIX}/{profile}@{record['registryDigest']}",
                    "qualificationSource": record["sourceCommit"],
                    "evidence": evidence["evidence"],
                    "monitoringStatus": "clean",
                }
            )
    return results


def main() -> None:
    """Check version-changing PRs or an explicitly selected package before upload."""
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--base", help="PR base commit")
    selection.add_argument("--package", help="Distribution being published")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        head = revision(args.root, args.head)
        if args.package:
            if not re.fullmatch(r"[a-z][a-z0-9-]+", args.package):
                raise ValueError("Invalid distribution name")
            metadata = document(args.root, head, f"packages/{args.package}/pyproject.toml")
            if metadata["project"]["name"] != args.package:
                raise ValueError("Distribution identity differs from selected package")
            packages = [args.package]
        else:
            packages = releasing(args.root, revision(args.root, args.base), head)
        results = check(args.root, head, packages)
        print(json.dumps({"kindImages": results, "required": bool(results)}, indent=2))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        message = (
            str(error) if isinstance(error, ValueError) else "Image readiness evidence unavailable"
        )
        parser.exit(1, f"Kind release blocked: {message}\n")


if __name__ == "__main__":
    main()
