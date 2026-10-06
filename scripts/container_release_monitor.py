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
    digest,
    key,
    monitoring_end,
    now,
    observe,
    read,
    scan_result,
    timestamp,
    validate_identity,
    write,
)
from container_release_assets import Evidence
from container_release_history import History
from container_release_publish import context, outputs
from container_release_registry import anonymous_pull, manifest
from container_release_status import MONITOR
from image_security_evidence import verify_inventory
from prepare_container_release import assess

BATCH_SIZE = 6


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
            targets.append({"candidate": candidate, "state": record["state"]})
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
        name = key(candidate)
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
        root = directory / "reports" / f"{candidate['profile']}-{candidate['version']}"
        root.mkdir(parents=True)
        image = f"{PREFIX}/{candidate['profile']}@{candidate['registryDigest']}"
        image_id = None
        result: dict[str, Any] = {
            "digest": candidate["registryDigest"],
            "monitorRunId": value["runId"],
            "monitorRunAttempt": value["runAttempt"],
            "outcome": "unavailable",
            "assessedAt": now(),
        }
        try:
            absent = False
            if target["state"] == "abandoned":
                absent = (
                    manifest(candidate["profile"], candidate["registryDigest"]) is None
                    and manifest(candidate["profile"], candidate["version"]) is None
                )
            if absent:
                result["absenceProof"] = {
                    "digest": candidate["registryDigest"],
                    "digestMissing": True,
                    "versionTagMissing": True,
                    "checkedAt": now(),
                }
            else:
                image_id = anonymous_pull(candidate)
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
        name = f"{candidate['profile']}-{candidate['version']}"
        root = directory / "reports" / name
        observation = root / "observation.json"
        if observation.is_file() and not observation.is_symlink():
            result = read(observation)
            assessment = result["assessment"]
            if (
                result.get("candidate") != candidate
                or assessment.get("digest") != candidate["registryDigest"]
                or assessment.get("monitorRunId") != run_id
                or assessment.get("monitorRunAttempt") != int(attempt)
                or timestamp(assessment.get("assessedAt")) < timestamp(value["startedAt"])
                or timestamp(assessment["assessedAt"]) > timestamp(now())
            ):
                raise ValueError("Monitor result differs from its planned attempt")
        else:
            assessment = {
                "digest": candidate["registryDigest"],
                "monitorRunId": run_id,
                "monitorRunAttempt": int(attempt),
                "outcome": "unavailable",
                "assessedAt": now(),
                "reason": "Monitor worker did not deliver an observation",
            }
            root.mkdir(parents=True, exist_ok=True)
            write(observation, {"candidate": candidate, "assessment": assessment})
        if assessment.get("outcome") in {"clean", "vulnerable"}:
            verify_inventory(read(root / "sbom.syft.json"), candidate["imageId"])
            verified = scan_result(
                read(root / "grype.json"), candidate["imageId"], assessment["assessedAt"]
            )
            if any(assessment.get(field) != expected for field, expected in verified.items()):
                raise ValueError("Monitor result differs from its retained scanner report")
            if read(root / "assessment.json") != verified:
                raise ValueError("Monitor assessment differs from its scanner evidence")
        elif assessment.get("outcome") != "unavailable":
            raise ValueError("Monitor worker did not finish its assessment")
        assessments[key(candidate)] = assessment
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
            current = result["releases"][name]
            expected = next(t["candidate"] for t in value["targets"] if key(t["candidate"]) == name)
            if current["candidate"] != expected:
                raise ValueError("Release identity changed during monitoring")
            assessment = assessment | {"evidenceRelease": evidence_tag}
            proof = assessment.get("absenceProof")
            if proof and current["state"] == "abandoned":
                current.update(publicExposure="absent", absenceProof=proof)
            result = observe(result, name, assessment)
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
