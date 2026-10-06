"""Persist monitoring attempts before scanning every active published-image digest."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from container_release import (
    PREFIX,
    assessment_status,
    decode,
    digest,
    key,
    monitoring_end,
    now,
    observe,
    read,
    require_digest,
    scan_result,
    timestamp,
    unexpected_end,
    validate_identity,
    write,
)
from container_release_assets import Evidence
from container_release_history import History, sha256
from container_release_oci import MANIFEST
from container_release_publish import context, outputs
from container_release_registry import anonymous_pull, manifest
from container_release_status import MONITOR
from image_security_evidence import verify_inventory
from prepare_container_release import assess

BATCH_SIZE = 6


def target_name(target: dict[str, Any]) -> str:
    """Keep unexpected registry bytes separate from the approved candidate's evidence."""
    candidate = target["candidate"]
    suffix = (
        "-" + target["unexpectedDigest"].removeprefix("sha256:")
        if "unexpectedDigest" in target
        else ""
    )
    return f"{candidate['profile']}-{candidate['version']}{suffix}"


def target_digest(target: dict[str, Any]) -> str:
    """Select an independently monitored digest without changing the approved identity."""
    return require_digest(target.get("unexpectedDigest", target["candidate"]["registryDigest"]))


def monitored(record: dict[str, Any], at: str) -> bool:
    """Incomplete and abandoned candidates stay active until retirement is independently proven."""
    end = monitoring_end(record)
    return end is None or timestamp(at) < end


def begin(directory: Path) -> None:
    """Record running outcomes before starting scanners so an interruption cannot preserve green."""
    source, run_id, attempt = context(MONITOR)
    at = now()
    targets: list[dict[str, Any]] = []

    def start(catalogue: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(catalogue)
        for name, record in sorted(catalogue["releases"].items()):
            if not monitored(record, at):
                continue
            candidate = record["candidate"]
            validate_identity(candidate)
            current = result["releases"][name]
            if record["state"] != "completed":
                try:
                    raw = manifest(candidate["profile"], candidate["version"])
                    current["registryDiscovery"] = {"checkedAt": at, "outcome": "checked"}
                    if raw is not None and sha256(raw) != candidate["registryDigest"]:
                        found = sha256(raw)
                        entry = current.setdefault("unexpectedDigests", {}).setdefault(
                            found, {"digest": found, "discoveredAt": at}
                        )
                        entry.pop("absenceProof", None)
                except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
                    current["registryDiscovery"] = {"checkedAt": at, "outcome": "unavailable"}
            targets.append({"candidate": candidate, "state": record["state"]})
            for found, entry in sorted(current.get("unexpectedDigests", {}).items()):
                if unexpected_end(entry) is not None:
                    continue
                targets.append(
                    {"candidate": candidate, "state": record["state"], "unexpectedDigest": found}
                )
                entry["latestAttempt"] = {
                    "digest": found,
                    "assessedAt": at,
                    "outcome": "running",
                    "monitorRunId": run_id,
                    "monitorRunAttempt": int(attempt),
                }
            result = observe(
                result,
                name,
                {
                    "digest": record["registryDigest"],
                    "assessedAt": at,
                    "outcome": "running",
                    "monitorRunId": run_id,
                    "monitorRunAttempt": int(attempt),
                },
            )
        if len(targets) > 256 * BATCH_SIZE:
            raise ValueError("Monitored set exceeds the qualified workflow batch limit")
        return result

    snapshot = History().append(
        start, operation=f"{run_id}/{attempt}/monitor-start", source=source, at=at
    )
    plan = {
        "runId": run_id,
        "runAttempt": int(attempt),
        "startedAt": at,
        "targets": targets,
        "catalogue": snapshot.reference if snapshot else None,
    }
    write(directory / "plan.json", plan)
    batches = [{"batch": n} for n in range((len(targets) + BATCH_SIZE - 1) // BATCH_SIZE)]
    outputs({"has_targets": str(bool(targets)).lower(), "matrix": json.dumps({"include": batches})})


def plan(directory: Path) -> dict[str, Any]:
    """Bind transferred scan inputs to this protected monitor workflow attempt."""
    _, run_id, attempt = context(MONITOR)
    value = read(directory / "plan.json")
    if value.get("runId") != run_id or value.get("runAttempt") != int(attempt):
        raise ValueError("Monitor plan belongs to another workflow attempt")
    targets = value.get("targets")
    if not isinstance(targets, list) or len(targets) > 256 * BATCH_SIZE:
        raise ValueError("Invalid monitor target set")
    seen = set()
    for target in targets:
        candidate = target["candidate"]
        validate_identity(candidate)
        target_digest(target)
        if "unexpectedDigest" in target and (
            target["unexpectedDigest"] == candidate["registryDigest"]
            or target.get("state") == "completed"
        ):
            raise ValueError("Unexpected monitor target cannot claim approved identity")
        name = target_name(target)
        if name in seen or target.get("state") not in {"incomplete", "completed", "abandoned"}:
            raise ValueError("Ambiguous monitor target")
        seen.add(name)
    return value


def scan(directory: Path, batch: int) -> None:
    """Scan exact anonymous registry bytes without executing the image's entrypoint."""
    value = plan(directory)
    if not 0 <= batch < (len(value["targets"]) + BATCH_SIZE - 1) // BATCH_SIZE:
        raise ValueError("Unknown monitor batch")
    for target in value["targets"][batch * BATCH_SIZE : (batch + 1) * BATCH_SIZE]:
        candidate = target["candidate"]
        root = directory / "reports" / target_name(target)
        root.mkdir(parents=True)
        selected = target_digest(target)
        image = f"{PREFIX}/{candidate['profile']}@{selected}"
        image_id = None
        result: dict[str, Any] = {
            "digest": selected,
            "monitorRunId": value["runId"],
            "monitorRunAttempt": value["runAttempt"],
            "outcome": "unavailable",
            "assessedAt": now(),
        }
        try:
            absent = False
            selected_candidate = candidate
            if "unexpectedDigest" in target:
                raw = manifest(candidate["profile"], selected)
                absent = raw is None
                if raw is not None:
                    if sha256(raw) != selected:
                        raise ValueError("Unexpected registry digest changed bytes")
                    (root / "manifest.json").write_bytes(raw)
                    document = decode(raw)
                    if document.get("mediaType") != MANIFEST:
                        raise ValueError("Unexpected image is not a supported single manifest")
                    config = require_digest(document.get("config", {}).get("digest"))
                    result["imageId"] = config
                    selected_candidate = candidate | {
                        "registryDigest": selected,
                        "assessedManifestDigest": selected,
                        "imageId": config,
                    }
            elif target["state"] == "abandoned":
                absent = (
                    manifest(candidate["profile"], candidate["registryDigest"]) is None
                    and manifest(candidate["profile"], candidate["version"]) is None
                )
            if absent:
                result["absenceProof"] = {
                    "digest": selected,
                    "digestMissing": True,
                    **({"versionTagMissing": True} if "unexpectedDigest" not in target else {}),
                    "checkedAt": now(),
                }
            else:
                image_id = anonymous_pull(selected_candidate)
                result.update(assess(root, image_id, require_clean=False))
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
            result.update(
                outcome="unavailable",
                assessedAt=now(),
                reason="Registry or scanner assessment unavailable",
            )
        finally:
            if image_id:
                try:
                    subprocess.run(
                        ["docker", "image", "rm", image],
                        capture_output=True,
                        check=False,
                        timeout=60,
                    )
                except (OSError, subprocess.SubprocessError):
                    # Best-effort cleanup must not discard the completed assessment.
                    pass
        write(root / "observation.json", {"candidate": candidate, "assessment": result})


def finish(directory: Path) -> None:
    """Retain reports before recording outcomes; missing worker results become explicit failures."""
    source, run_id, attempt = context(MONITOR)
    value = plan(directory)
    retained = directory / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    assessments: dict[str, dict[str, Any]] = {}
    members: dict[str, str] = {}
    for number, target in enumerate(value["targets"]):
        candidate = target["candidate"]
        name = target_name(target)
        root = directory / "reports" / name
        observation = root / "observation.json"
        if observation.is_file() and not observation.is_symlink():
            result = read(observation)
            assessment = result["assessment"]
            if (
                result.get("candidate") != candidate
                or assessment.get("digest") != target_digest(target)
                or assessment.get("monitorRunId") != run_id
                or assessment.get("monitorRunAttempt") != int(attempt)
                or timestamp(assessment.get("assessedAt")) < timestamp(value["startedAt"])
                or timestamp(assessment["assessedAt"]) > timestamp(now())
            ):
                raise ValueError("Monitor result differs from its planned attempt")
        else:
            assessment = {
                "digest": target_digest(target),
                "monitorRunId": run_id,
                "monitorRunAttempt": int(attempt),
                "outcome": "unavailable",
                "assessedAt": now(),
                "reason": "Monitor worker did not deliver an observation",
            }
            root.mkdir(parents=True, exist_ok=True)
            write(observation, {"candidate": candidate, "assessment": assessment})
        if assessment.get("outcome") in {"clean", "vulnerable"}:
            image_id = candidate["imageId"]
            if "unexpectedDigest" in target:
                raw_manifest = root / "manifest.json"
                if raw_manifest.is_symlink() or digest(raw_manifest) != target_digest(target):
                    raise ValueError("Unexpected image evidence differs from its digest")
                document = read(raw_manifest)
                image_id = require_digest(document.get("config", {}).get("digest"))
                if document.get("mediaType") != MANIFEST or assessment.get("imageId") != image_id:
                    raise ValueError("Unexpected image configuration differs from its assessment")
            verify_inventory(read(root / "sbom.syft.json"), image_id)
            verified = scan_result(read(root / "grype.json"), image_id, assessment["assessedAt"])
            if any(assessment.get(field) != expected for field, expected in verified.items()):
                raise ValueError("Monitor result differs from its retained scanner report")
            if read(root / "assessment.json") != verified:
                raise ValueError("Monitor assessment differs from its scanner evidence")
        elif assessment.get("outcome") != "unavailable":
            raise ValueError("Monitor worker did not finish its assessment")
        assessments[name] = assessment
        archive = retained / f"reports-{number // BATCH_SIZE:04d}.zip"
        with zipfile.ZipFile(
            archive, "w" if number % BATCH_SIZE == 0 else "a", zipfile.ZIP_DEFLATED
        ) as bundle:
            for path in sorted(root.iterdir()):
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.name
                    not in {
                        "observation.json",
                        "assessment.json",
                        "sbom.syft.json",
                        "sbom.spdx.json",
                        "grype.json",
                        "grype.yaml",
                        "manifest.json",
                    }
                ):
                    raise ValueError("Unexpected monitor evidence file")
                member = f"{name}/{path.name}"
                members[member] = digest(path)
                info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with (
                    path.open("rb") as source_file,
                    bundle.open(info, "w", force_zip64=True) as target_file,
                ):
                    shutil.copyfileobj(source_file, target_file)
    evidence_tag = f"image-monitor-{run_id}-{attempt}"
    if assessments:
        write(
            retained / "monitor-index.json",
            {"runId": run_id, "runAttempt": int(attempt), "files": members},
        )
        files = {path.name: digest(path) for path in retained.iterdir() if path.is_file()}
        evidence = Evidence()
        release = evidence.ensure(evidence_tag, source)
        evidence.retain(release, retained, files)
        evidence.publish(release, source)
    at = now()

    def record(catalogue: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(catalogue)
        for name, assessment in assessments.items():
            target = next(t for t in value["targets"] if target_name(t) == name)
            expected = target["candidate"]
            current = result["releases"][key(expected)]
            if current["candidate"] != expected:
                raise ValueError("Release identity changed during monitoring")
            assessment = assessment | {"evidenceRelease": evidence_tag}
            proof = assessment.get("absenceProof")
            if "unexpectedDigest" in target:
                entry = current["unexpectedDigests"][target["unexpectedDigest"]]
                entry["latestAttempt"] = assessment
                if assessment["outcome"] == "vulnerable":
                    entry["lastKnownVulnerable"] = copy.deepcopy(assessment)
                if proof:
                    entry["absenceProof"] = proof
                continue
            if proof and current["state"] == "abandoned":
                current.update(publicExposure="absent", absenceProof=proof)
            result = observe(result, key(expected), assessment)
        return result

    snapshot = History().append(
        record, operation=f"{run_id}/{attempt}/monitor-finish", source=source, at=at
    )
    statuses = (
        [assessment_status(r, at) for r in snapshot.catalogue["releases"].values()]
        if snapshot
        else []
    )
    outputs(
        {
            "attention": str(
                any(status in {"vulnerable", "unavailable", "stale"} for status in statuses)
            ).lower()
        }
    )


def main() -> None:
    """Keep catalogue mutation jobs separate from scanner jobs without write credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("begin", "scan", "finish"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--batch", type=int)
    args = parser.parse_args()
    if args.stage == "scan":
        if args.batch is None:
            raise ValueError("A scan requires its planned batch number")
        scan(args.directory, args.batch)
    else:
        {"begin": begin, "finish": finish}[args.stage](args.directory)


if __name__ == "__main__":
    main()
