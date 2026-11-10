"""Acquire current Go-image evidence from authenticated releases and completed monitoring."""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
import zipfile
from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

from container_release import REPOSITORY, digest, now, read, require_digest, timestamp, write
from container_release_assets import Evidence
from container_release_history import GitHub, History, HistoryAdvanced, decode, positive_id
from container_release_status import MONITOR, latest_monitor
from image_release_needed import git, release_evidence
from image_security_evidence import PROVIDER_MANIFESTS
from plan_go_remediation import evidence, plan
from select_image_security import PROFILES

FILES = {"observation.json", "assessment.json", "sbom.syft.json", "grype.json"}
MAX_MEMBER = 512 * 1024**2


class EvidenceChanging(ValueError):
    """Remediation must restart against a newer source or evidence snapshot."""


def catalogue_images(catalogue: dict[str, Any], profiles: Iterable[str]) -> dict[str, Any]:
    """Bind current releases and their assessments, including unpublished inherited profiles."""
    selected = set(profiles)
    if not selected or not selected <= set(PROFILES):
        raise ValueError("Invalid remediation image scope")
    result = {}
    for profile in sorted(selected):
        name = catalogue["current"].get(profile)
        result[profile] = (
            {"release": name, "record": deepcopy(catalogue["releases"][name])}
            if name is not None
            else None
        )
    return result


def require_image_scope(receipt: dict[str, Any], profiles: list[str]) -> None:
    """A retained scope cannot omit any profile required by the recomputed plan."""
    if (
        "images" in receipt
        and profiles
        and (not isinstance(receipt["images"], dict) or set(receipt["images"]) != set(profiles))
    ):
        raise ValueError("Remediation image scope differs from the plan")


def deferred(
    directory: Path, error: EvidenceChanging | HistoryAdvanced, phase: str
) -> dict[str, Any]:
    """Retain a retryable refusal without claiming publication or discarding completed checks."""
    result = {"status": "deferred", "phase": phase, "reason": str(error)}
    write(directory / "deferral.json", result)
    message = f"Remediation {phase} deferred: {error}. Retry with fresh source and evidence."
    print(message, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(f"\n## Image remediation: deferred\n\n{message}\n")
    return result


def main_head(client: GitHub) -> str:
    """Read the mutable base once, then bind all work to its immutable commit."""
    value = decode(client.request(f"repos/{REPOSITORY}/git/ref/heads/main"))["object"]
    if value.get("type") != "commit" or not re.fullmatch(r"[0-9a-f]{40}", value.get("sha", "")):
        raise ValueError("Invalid main branch identity")
    return value["sha"]


def monitor_source(client: GitHub, monitor: dict[str, Any]) -> str:
    """Require a completed trusted-main attempt with durable monitor results."""
    if monitor.get("status") in {"queued", "in_progress", "pending", "requested", "waiting"}:
        raise EvidenceChanging("Latest monitoring attempt is still running or queued")
    if monitor.get("status") != "completed" or not (
        monitor.get("conclusion") == "success"
        or monitor.get("conclusion") == "failure"
        and monitor.get("stateDeliveryVerified") is True
    ):
        raise ValueError("Latest monitoring attempt has no verified delivery")
    run = decode(
        client.request(
            f"repos/{REPOSITORY}/actions/runs/{positive_id(monitor['id'])}/attempts/"
            f"{positive_id(monitor['run_attempt'])}"
        )
    )
    if (
        run.get("id") != monitor["id"]
        or run.get("run_attempt") != monitor["run_attempt"]
        or run.get("path") != f".github/workflows/{MONITOR}"
        or run.get("head_branch") != "main"
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("event") not in {"schedule", "workflow_dispatch", "workflow_run"}
        or run.get("status") != "completed"
        or run.get("conclusion") != monitor["conclusion"]
        or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))
    ):
        raise ValueError("Monitor evidence has an unexpected producer")
    return run["head_sha"]


def restore_monitor(
    client: GitHub, monitor: dict[str, Any], source: str, targets: set[str], directory: Path
) -> None:
    """Extract only indexed, bounded evidence members from the immutable monitor release."""
    retained = Evidence(client)
    tag = f"image-monitor-{monitor['id']}-{monitor['run_attempt']}"
    release = retained.find(tag)
    if (
        release is None
        or release.get("draft") is not False
        or release.get("immutable") is not True
        or client.tag_commit(tag) != source
    ):
        raise ValueError("Monitor evidence is not immutable at its producer commit")
    assets = client.assets(positive_id(release["id"]))
    names = [asset.get("name") for asset in assets]
    if len(names) != len(set(names)) or len(assets) > 257:
        raise ValueError("Ambiguous or oversized monitor asset set")
    indexes = [asset for asset in assets if asset.get("name") == "monitor-index.json"]
    if len(indexes) != 1 or not 0 < indexes[0].get("size", 0) <= 32 * 1024**2:
        raise ValueError("Monitor evidence has no unique index")
    directory.mkdir(parents=True)
    index_path = directory / "monitor-index.json"
    retained.download(indexes[0], index_path, require_digest(indexes[0].get("digest")))
    index = read(index_path)
    if (
        index.get("runId") != str(monitor["id"])
        or index.get("runAttempt") != monitor["run_attempt"]
    ):
        raise ValueError("Monitor index identifies another attempt")
    members = index.get("files")
    if not isinstance(members, dict) or len(members) > 16384:
        raise ValueError("Invalid monitor index")
    wanted = {f"{target}/{name}" for target in targets for name in FILES}
    if not wanted <= members.keys():
        raise ValueError("Current image evidence is missing from the monitor index")
    for name in wanted:
        require_digest(members[name])
    found = set()
    downloaded = 0
    for asset in assets:
        if not re.fullmatch(r"reports-[0-9]{4}\.zip", str(asset.get("name"))):
            continue
        archive = directory / asset["name"]
        size = asset.get("size")
        if type(size) is not int or size <= 0:
            raise ValueError("Invalid monitor archive size")
        downloaded += size
        if downloaded > 4 * 1024**3:
            raise ValueError("Monitor archives exceed the acquisition size limit")
        retained.download(asset, archive, require_digest(asset.get("digest")))
        try:
            with zipfile.ZipFile(archive) as bundle:
                if len(bundle.infolist()) > 16384:
                    raise ValueError("Monitor archive has too many members")
                for member in bundle.infolist():
                    if member.filename not in wanted:
                        continue
                    if (
                        member.filename in found
                        or member.orig_filename != member.filename
                        or stat.S_ISLNK(member.external_attr >> 16)
                        or not 0 < member.file_size <= MAX_MEMBER
                    ):
                        raise ValueError("Unsafe or duplicate monitor member")
                    target = (directory / member.filename).resolve()
                    if not target.is_relative_to(directory.resolve()):
                        raise ValueError("Monitor member escapes its evidence directory")
                    target.parent.mkdir(exist_ok=True)
                    with bundle.open(member) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    if (
                        target.stat().st_size != member.file_size
                        or digest(target) != members[member.filename]
                    ):
                        raise ValueError("Monitor member differs from its immutable index")
                    found.add(member.filename)
        finally:
            archive.unlink()
        if found == wanted:
            break
    if found != wanted:
        raise ValueError("Monitor archives did not deliver every current image")


def current(client: GitHub, receipt: dict[str, Any]) -> None:
    """Refuse stale base, catalogue or monitor attempts before a proposal can be published."""
    if "rehearsalFixture" in receipt:
        raise ValueError("Historical findings are not a current-evidence receipt")
    if (
        not timedelta(0)
        <= timestamp(now()) - timestamp(receipt["acquiredAt"])
        < timedelta(hours=24)
    ):
        raise ValueError("Remediation evidence receipt expired")
    if (
        not timedelta(0)
        <= timestamp(now()) - timestamp(receipt["oldestAssessment"])
        < timedelta(hours=48)
    ):
        raise ValueError("Retained monitoring assessment expired")
    if main_head(client) != receipt["source"]:
        raise EvidenceChanging("Main changed during remediation")
    snapshot = History(client).head()
    if snapshot is None:
        raise ValueError("No completed image release catalogue")
    if "images" in receipt:
        images = receipt["images"]
        if not isinstance(images, dict):
            raise ValueError("Invalid remediation image scope")
        if catalogue_images(snapshot.catalogue, images) != images:
            raise EvidenceChanging(
                "Qualified image releases or assessments changed during remediation"
            )
    elif snapshot.reference != receipt["catalogue"]:
        raise EvidenceChanging("Release history changed during remediation")
    if latest_monitor(client) != receipt["monitor"]:
        raise EvidenceChanging("Monitoring changed during remediation")


def acquire(
    client: GitHub,
    root: Path,
    directory: Path,
    *,
    selected_profiles: Iterable[str] | None = None,
    planner: Callable[[Path, list[Path]], dict[str, Any]] | None = None,
    require_all_profiles: bool = True,
) -> dict[str, Any]:
    """Authenticate current released images and fresh monitoring before planning source edits."""
    selected = set(PROVIDER_MANIFESTS if selected_profiles is None else selected_profiles)
    unknown = selected - set(PROFILES)
    if unknown:
        raise ValueError(f"Unknown image profiles: {', '.join(sorted(unknown))}")
    source = git(root, "rev-parse", "HEAD").strip()
    if main_head(client) != source:
        raise EvidenceChanging("Evidence acquisition requires current main")
    monitor = latest_monitor(client)
    producer = monitor_source(client, monitor)
    snapshot = History(client).head()
    if snapshot is None:
        raise ValueError("No completed image release catalogue")
    if latest_monitor(client) != monitor:
        raise EvidenceChanging("Monitoring changed during acquisition")
    records = {}
    unpublished = []
    at = now()
    for profile in sorted(selected):
        name = snapshot.catalogue["current"].get(profile)
        if name is None:
            if not require_all_profiles:
                unpublished.append(profile)
                continue
            raise ValueError(f"A supported current image release is missing: {profile}")
        record = snapshot.catalogue["releases"][name]
        if record["profile"] != profile:
            raise ValueError("Current image catalogue profile is inconsistent")
        attempt = record.get("latestAttempt", {})
        age = timestamp(at) - timestamp(attempt.get("assessedAt"))
        if (
            attempt.get("monitorRunId") != str(monitor["id"])
            or attempt.get("monitorRunAttempt") != monitor["run_attempt"]
            or attempt.get("digest") != record["registryDigest"]
            or attempt.get("outcome") not in {"clean", "vulnerable"}
            or attempt.get("evidenceRelease")
            != f"image-monitor-{monitor['id']}-{monitor['run_attempt']}"
            or not timedelta(0) <= age < timedelta(hours=48)
            or record.get("unexpectedDigests")
            or record.get("registryDiscovery", {}).get("outcome") == "unavailable"
        ):
            raise ValueError("Current monitoring is stale, unavailable or contradictory")
        release_evidence(client, record, directory / "releases" / profile)
        records[f"{profile}-{record['version']}"] = record
    if not records:
        raise ValueError("No supported current image releases are available")
    restored = directory / "evidence"
    restore_monitor(client, monitor, producer, set(records), restored)
    paths = []
    for name, record in records.items():
        path = restored / name
        observed = read(path / "observation.json")
        if observed.get("candidate") != record["candidate"] or observed.get("assessment") != {
            key: value for key, value in record["latestAttempt"].items() if key != "evidenceRelease"
        }:
            raise ValueError("Monitor bytes differ from the authoritative catalogue")
        identity, _, _ = evidence(path)
        if identity["registryDigest"] != record["registryDigest"]:
            raise ValueError("Planner evidence identifies another image")
        paths.append(path)
    receipt = {
        "schemaVersion": 1,
        "source": source,
        "catalogue": snapshot.reference,
        "monitor": monitor,
        "directories": [p.name for p in paths],
        "acquiredAt": at,
        "oldestAssessment": min(
            record["latestAttempt"]["assessedAt"] for record in records.values()
        ),
    }
    if not require_all_profiles:
        receipt["unpublishedProfiles"] = unpublished
    result = (plan if planner is None else planner)(root, paths)
    receipt["images"] = catalogue_images(snapshot.catalogue, result["affectedProfiles"] or selected)
    current(client, receipt)
    write(directory / "receipt.json", receipt)
    write(directory / "plan.json", result)
    return result


def acquire_with_retry(
    client: GitHub,
    root: Path,
    directory: Path,
    *,
    selected_profiles: Iterable[str] | None = None,
    planner: Callable[[Path, list[Path]], dict[str, Any]] | None = None,
    require_all_profiles: bool = True,
) -> dict[str, Any] | None:
    """Retry changing snapshots in isolated directories; defer to the next trigger if still busy."""
    selected = None if selected_profiles is None else tuple(selected_profiles)
    directory.mkdir(parents=True, exist_ok=False)
    for attempt in range(1, 4):
        staging = directory / "attempts" / str(attempt)
        try:
            result = acquire(
                client,
                root,
                staging,
                selected_profiles=selected,
                planner=planner,
                require_all_profiles=require_all_profiles,
            )
        except (EvidenceChanging, HistoryAdvanced) as error:
            write(staging / "deferred.json", {"attempt": attempt, "reason": str(error)})
            print(f"Acquisition attempt {attempt}/3 deferred: {error}", flush=True)
            if attempt < 3:
                time.sleep(30)
        else:
            for child in staging.iterdir():
                child.rename(directory / child.name)
            return result
    return None
