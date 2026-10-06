"""Run separately permissioned stages of the protected container-image release workflow."""

from __future__ import annotations

import argparse
import copy
import os
import re
from pathlib import Path
from typing import Any

from build_hyperlight_aks_image import SOURCE_URL
from check_container_release_image import check
from container_release import (
    IDENTITY_FIELDS,
    PREFIX,
    REPOSITORY,
    abandon,
    complete,
    completion_predicate,
    digest,
    key,
    now,
    read,
    require_digest,
    reserve,
    validate_identity,
    write,
)
from container_release_assets import Evidence, image_tag, indexed_files
from container_release_history import GitHub, History, decode, positive_id
from container_release_registry import anonymous_pull, promote
from prepare_container_release import run
from verify_container_release import verify_candidate, verify_evidence, verify_identity


def context(workflow: str = "container-image-release.yml") -> tuple[str, str, str]:
    """Bind mutation stages to the exact protected-main workflow and checked-out source."""
    source = os.environ.get("GITHUB_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{REPOSITORY}/.github/workflows/{workflow}@refs/heads/main"
        or not re.fullmatch(r"[0-9a-f]{40}", source)
        or not re.fullmatch(r"[1-9][0-9]*", run_id)
        or not re.fullmatch(r"[1-9][0-9]*", attempt)
    ):
        raise ValueError("Release mutation requires the protected workflow identity")
    if run(["git", "rev-parse", "HEAD"], capture_output=True).stdout.strip() != source:
        raise ValueError("Workflow checkout differs from its signing source")
    return source, run_id, attempt


def selected(directory: Path) -> dict[str, Any]:
    """Require the originating candidate, including during a rerun of the same workflow."""
    source, run_id, _ = context()
    candidate = read(directory / "candidate.json")
    validate_identity(candidate)
    approved = require_digest(os.environ.get("APPROVED_CANDIDATE_SHA256"))
    if digest(directory / "candidate.json") != approved:
        raise ValueError("Candidate differs from the identity presented for approval")
    if candidate["sourceCommit"] != source or candidate["attemptId"] != run_id:
        raise ValueError("Candidate belongs to another workflow run or source")
    return candidate


def outputs(values: dict[str, Any]) -> None:
    """Expose validated scalar identities to later jobs without workflow-command interpolation."""
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            text = str(value)
            if "\n" in text or "\r" in text:
                raise ValueError("Invalid multiline workflow output")
            stream.write(f"{name}={text}\n")


def committed(candidate: dict[str, Any]) -> dict[str, Any]:
    """Retrieve a completion from authoritative state before signing or delivering it."""
    head = History().head()
    if head is None:
        raise ValueError("Release catalogue is unavailable")
    record = head.catalogue["releases"].get(key(candidate))
    if not isinstance(record, dict) or any(
        record.get(field) != candidate[field] for field in IDENTITY_FIELDS
    ):
        raise ValueError("Catalogue release identity does not match the candidate")
    completion_predicate(record)
    return record


def reserve_version(directory: Path) -> None:
    """Commit pre-write registration while holding the short catalogue writer lock."""
    candidate = selected(directory)
    source, run_id, attempt = context()
    at = now()
    head = History().append(
        lambda catalogue: reserve(catalogue, candidate, at),
        operation=f"{run_id}/{attempt}/reserve",
        source=source,
        at=at,
    )
    if head is None:
        raise ValueError("Reservation did not commit")
    record = head.catalogue["releases"][key(candidate)]
    outputs(
        {
            "completed": str(record["state"] == "completed").lower(),
            "delivered": str(record["delivery"] == "complete").lower(),
            "digest": candidate["registryDigest"],
            "image": f"{PREFIX}/{candidate['profile']}",
        }
    )


def qualify(directory: Path) -> None:
    """Authenticate two candidate attestations before a read-only runner executes image code."""
    candidate = selected(directory)
    identity = verify_candidate(candidate, directory)
    image_id = anonymous_pull(candidate)
    runtime = check(candidate["profile"], image_id, directory)
    if candidate["profile"] == "hyperlight":
        source = runtime.get("result", {}).get("source")
        if (
            not isinstance(source, dict)
            or source.get("repository") != SOURCE_URL
            or source.get("revision") != candidate["sourceCommit"]
            or source.get("dirty") is not False
        ):
            raise ValueError("Qualified Hyperlight payload differs from the selected clean source")
    write(
        directory / "qualification.json",
        {
            "candidate": candidate,
            "identity": identity,
            "runtime": runtime,
            "anonymousPullVerified": True,
            "qualifiedAt": now(),
            "runId": candidate["attemptId"],
        },
    )


def retain_and_complete(directory: Path) -> Path:
    """Retain indexed evidence before committing completion; resume committed releases unchanged."""
    candidate = selected(directory)
    source, run_id, attempt = context()
    history = History()
    head = history.head()
    if head is None or key(candidate) not in head.catalogue["releases"]:
        raise ValueError("Completion requires a durable reservation")
    evidence = Evidence()
    if head.catalogue["releases"][key(candidate)]["state"] == "completed":
        record = committed(candidate)
        restored = directory / "retained"
        evidence.fetch(image_tag(candidate), restored)
        verify_evidence(restored, record["evidenceIndexSha256"])
        directory = restored
    else:
        existing = evidence.find(image_tag(candidate))
        if existing and any(
            a.get("name") == "evidence-index.json" for a in evidence.github.assets(existing["id"])
        ):
            restored = directory / "retained"
            evidence.fetch(image_tag(candidate), restored)
            verify_evidence(restored, digest(restored / "evidence-index.json"))
            if read(restored / "candidate.json") != candidate:
                raise ValueError("Retained qualification identifies another candidate")
            directory = restored
        qualified = read(directory / "qualification.json")
        if (
            qualified.get("candidate") != candidate
            or qualified.get("runId") != run_id
            or qualified.get("anonymousPullVerified") is not True
            or qualified.get("identity", {}).get("candidateIdentityVerified") is not True
        ):
            raise ValueError("Missing independent qualification for this candidate")
        verify_candidate(candidate, directory)
        files = {}
        for path in directory.iterdir():
            if path.is_file() and path.name not in {
                "evidence-index.json",
                "completion.jsonl",
                "completion-predicate.json",
            }:
                if path.is_symlink():
                    raise ValueError("Evidence cannot contain symlinks")
                files[path.name] = digest(path)
        write(directory / "evidence-index.json", files)
        evidence_hash = digest(directory / "evidence-index.json")
        verify_evidence(directory, evidence_hash)
        release = evidence.ensure(image_tag(candidate), source)
        # Upload the index last: its presence is the durable recovery checkpoint.
        evidence.retain(release, directory, indexed_files(directory))
        at = now()
        history.append(
            lambda catalogue: complete(catalogue, candidate, evidence_hash, at),
            operation=f"{run_id}/{attempt}/complete",
            source=source,
            at=at,
        )
        record = committed(candidate)
    write(directory / "completion-predicate.json", completion_predicate(record))
    outputs(
        {
            "directory": directory.resolve(),
            "digest": candidate["registryDigest"],
            "image": f"{PREFIX}/{candidate['profile']}",
        }
    )
    return directory


def deliver(directory: Path) -> None:
    """Verify and freeze completion evidence before marking its delivery complete."""
    candidate = selected(directory)
    source, run_id, attempt = context()
    record = committed(candidate)
    if digest(directory / "evidence-index.json") != record["evidenceIndexSha256"]:
        raise ValueError("Delivery index differs from committed completion")
    evidence = Evidence()
    release = evidence.ensure(image_tag(candidate), source)
    existing = [
        a for a in evidence.github.assets(release["id"]) if a.get("name") == "completion.jsonl"
    ]
    if len(existing) > 1:
        raise ValueError("Ambiguous completion evidence")
    if existing:
        # Different valid signature bundles can attest the same immutable completion predicate.
        raw = evidence.github.download(positive_id(existing[0]["id"]))
        (directory / "completion.jsonl").write_bytes(raw)
    verify_identity(
        candidate | {"evidenceIndexSha256": record["evidenceIndexSha256"]}, directory, bundles=True
    )
    files = indexed_files(directory) | {"completion.jsonl": digest(directory / "completion.jsonl")}
    evidence.retain(release, directory, files)
    evidence.publish(release, source)

    def delivered(catalogue: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(catalogue)
        selected_record = result["releases"][key(candidate)]
        if completion_predicate(selected_record) != completion_predicate(record):
            raise ValueError("Completion changed during evidence delivery")
        selected_record["delivery"] = "complete"
        return result

    History().append(delivered, operation=f"{run_id}/{attempt}/deliver", source=source, at=now())


def predicate(directory: Path) -> None:
    """Re-read committed state on the signing runner and bind every transferred evidence file."""
    candidate = selected(directory)
    record = committed(candidate)
    verify_evidence(directory, record["evidenceIndexSha256"])
    write(directory / "completion-predicate.json", completion_predicate(record))
    outputs({"digest": candidate["registryDigest"], "image": f"{PREFIX}/{candidate['profile']}"})


def approval(directory: Path) -> None:
    """Retain the actual protected-environment approval alongside the displayed assessment."""
    candidate = selected(directory)
    reviews = decode(
        GitHub().request(f"repos/{REPOSITORY}/actions/runs/{candidate['attemptId']}/approvals")
    )
    if not isinstance(reviews, list) or not any(
        review.get("state") == "approved"
        and any(
            environment.get("name") == "container-release"
            for environment in review.get("environments", [])
        )
        for review in reviews
    ):
        raise ValueError("No protected container-release approval was recorded")
    write(
        directory / "approval.json",
        {
            "candidate": candidate,
            "reviews": reviews,
            "sameBytesRefreshAuthorized": True,
            "displayedAssessmentSha256": digest(directory / "assessment.json"),
        },
    )


def main() -> None:
    """Run only the stage whose workflow job owns its required permissions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=("reserve", "promote", "qualify", "complete", "predicate", "deliver", "abandon"),
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--release")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.stage == "abandon":
        source, run_id, attempt = context()
        if not args.release or not args.reason:
            raise ValueError("Abandonment requires the exact reserved release and a reason")
        at = now()
        History().append(
            lambda c: abandon(c, args.release, args.reason, at),
            operation=f"{run_id}/{attempt}/abandon",
            source=source,
            at=at,
        )
    elif args.stage == "promote":
        approval(args.directory)
        candidate = promote(args.directory)
        outputs(
            {"digest": candidate["registryDigest"], "image": f"{PREFIX}/{candidate['profile']}"}
        )
    else:
        {
            "reserve": reserve_version,
            "qualify": qualify,
            "complete": retain_and_complete,
            "predicate": predicate,
            "deliver": deliver,
        }[args.stage](args.directory)


if __name__ == "__main__":
    main()
