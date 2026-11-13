"""Qualify platform updates and exercise isolated draft delivery without enabling automation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import image_remediation_pr as publication
import platform_rehearsal_history as history
import platform_remediation as platform
from container_release import REPOSITORY, ROOT, read, write
from container_release_history import GitHub, decode, encode, positive_id
from image_release_needed import git
from image_remediation import outputs, remaining
from image_remediation_evidence import acquire_with_retry, current
from plan_go_remediation import digest
from plan_platform_remediation import RUNTIME_RECIPES, acquire_plan

MANIFEST = "scripts/platform_rehearsal_cases.json"
WORKFLOW = "platform-image-remediation-rehearsal.yml"
CASES = ("current-rebuild", "bicep-update", "apk-pinned", "apk-unpinned", history.CASE)
UPDATE_CASES = {"bicep-update", "apk-pinned"}


class InterruptedDelivery(RuntimeError):
    """Stop after the signed branch exists but before creating its draft PR."""


class RehearsalGitHub(publication.BotGitHub):
    """Reserve one isolated draft branch for this manual workflow run."""

    title = publication.REHEARSAL_TITLE

    def __init__(self, run_id: int, paths: set[str] | None = None) -> None:
        self.paths = paths
        prefix = publication.REHEARSAL_BRANCH
        if paths is not None:
            prefix = publication.UPDATE_REHEARSAL_BRANCH
            self.title = publication.UPDATE_REHEARSAL_TITLE
        self.branch = prefix + str(positive_id(run_id))
        self.interrupt = False

    def request(self, endpoint: str, **kwargs: Any) -> bytes:
        if (
            self.paths is not None
            and kwargs.get("method", "GET") != "GET"
            and (endpoint == "graphql" or "/pulls" in endpoint)
        ):
            conflicts = platform.competing_pulls(self, self.paths)
            if conflicts:
                raise ValueError(f"Competing source PR appeared before delivery: {conflicts}")
        if (
            self.interrupt
            and endpoint == f"repos/{REPOSITORY}/pulls"
            and kwargs.get("method") == "POST"
        ):
            self.interrupt = False
            raise InterruptedDelivery("Signed branch retained before draft PR creation")
        return super().request(endpoint, **kwargs)


class AlteredCommit(GitHub):
    """Inject a changed read of a retained commit without mutating any hosted branch."""

    def __init__(self, client: RehearsalGitHub, head: str, mode: str) -> None:
        self.client, self.head, self.mode = client, head, mode
        self.branch, self.title = client.branch, client.title

    def request(self, endpoint: str, **kwargs: Any) -> bytes:
        if kwargs.get("method", "GET") != "GET":
            raise ValueError("Ownership refusal probes are read-only")
        raw = self.client.request(endpoint, **kwargs)
        if endpoint != f"repos/{REPOSITORY}/git/commits/{self.head}":
            return raw
        commit = copy.deepcopy(decode(raw))
        if self.mode == "unsigned":
            commit["message"] = self.title
        elif self.mode == "altered":
            commit["message"] = commit["message"].replace('"files":', '"alteredFiles":', 1)
        else:
            raise ValueError("Unknown ownership refusal probe")
        return encode(commit)


def request_record(root: Path) -> dict[str, Any]:
    """Bind transferred evidence to the exact manual main-branch workflow and requested case."""
    source = git(root, "rev-parse", "HEAD").strip()
    expected = {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": source,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/main",
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise ValueError("Rehearsal requires its manual workflow on trusted main")
    case = os.environ.get("REHEARSAL_CASE")
    if case not in CASES:
        raise ValueError("Unknown platform rehearsal case")
    return {
        "schemaVersion": 1,
        "purpose": "platform-remediation-rehearsal",
        "source": source,
        "runId": positive_id(int(os.environ["GITHUB_RUN_ID"])),
        "runAttempt": positive_id(int(os.environ["GITHUB_RUN_ATTEMPT"])),
        "case": case,
        "manifestSha256": digest(root / MANIFEST),
    }


def case_rows(root: Path, expected: dict[str, Any], case: str) -> list[dict[str, Any]]:
    """Select only authenticated findings matching the reviewed case contract."""
    manifest = read(root / MANIFEST)
    if manifest.get("schemaVersion") != 1 or set(manifest.get("cases", {})) != set(CASES):
        raise ValueError("Unsupported rehearsal case manifest")
    rule = manifest["cases"][case]
    rows = [
        row
        for row in expected["findings"]
        if row["type"] == rule["type"] and row["status"] == rule["findingStatus"]
    ]
    if not rows:
        raise ValueError(f"Requested rehearsal case {case} is unavailable in selected evidence")
    if rule["qualificationStatus"] == "release-needed" and expected["updates"]:
        raise ValueError("Rebuild-only rehearsal requires a plan without source edits")
    return rows


def acquire(root: Path, work: Path) -> dict[str, Any]:
    """Acquire the explicit finding case and current delivery authority without fallback."""
    request = request_record(root)
    expected = (
        history.acquire(root, work)
        if request["case"] == history.CASE
        else acquire_with_retry(
            GitHub(),
            root,
            work,
            selected_profiles=RUNTIME_RECIPES,
            planner=acquire_plan,
            require_all_profiles=False,
        )
    )
    write(work / "rehearsal-request.json", request)
    if expected is None:
        raise ValueError("Rehearsal acquisition deferred; retry after evidence stabilizes")
    case_rows(root, expected, request["case"])
    return expected


def validator(root: Path, work: Path):
    """Bind every worker to the dispatched case before selecting its evidence validator."""
    request = request_record(root)
    if read(work / "rehearsal-request.json") != request:
        raise ValueError("Rehearsal request belongs to another source, run, attempt or case")
    return history.validate if request["case"] == history.CASE else platform.validate


def authority(work: Path, case: str) -> dict[str, Any]:
    """Historical findings never replace the current receipt used at every delivery write."""
    return history.authority(work) if case == history.CASE else read(work / "receipt.json")


def qualify(root: Path, work: Path, results: Path) -> dict[str, Any]:
    """Require the chosen case, all ordinary profile checks and fresh production evidence."""
    request = request_record(root)
    validate = validator(root, work)
    expected, _ = validate(root, work)
    rows = case_rows(root, expected, request["case"])
    report = platform.qualify(root, work, results, validator=validate)
    rule = read(root / MANIFEST)["cases"][request["case"]]
    if report["status"] != rule["qualificationStatus"]:
        raise ValueError("Platform results do not satisfy the requested rehearsal case")
    observed: set[tuple[str, str]] = set()
    counts = {"baseline": 0, "candidate": 0}
    raw_counts = {"baseline": 0, "candidate": 0}
    for scan in report["scans"]:
        wanted = {
            (row["id"], row["component"]) for row in rows if platform.applies(row, scan["profile"])
        }
        matches = remaining(
            read(results / f"{scan['variant']}-{scan['profile']}" / "grype.json"), wanted
        )
        raw_counts[scan["variant"]] += len(matches)
        if scan["variant"] == "candidate":
            matches, _ = platform.vendor_resolutions(
                matches,
                rows,
                read(results / f"candidate-{scan['profile']}" / "sbom.syft.json"),
            )
        counts[scan["variant"]] += len(matches)
        if scan["variant"] == "baseline":
            observed.update(
                (row["vulnerability"]["id"], row["artifact"]["name"]) for row in matches
            )
    if counts["candidate"] or (
        rule["requireBaselineMatch"] and observed != {(row["id"], row["component"]) for row in rows}
    ):
        raise ValueError("Requested finding removal was not demonstrated")
    current(GitHub(), authority(work, request["case"]))
    return {
        **request,
        "status": "qualified",
        "scope": "controlled-rehearsal",
        "productionRecovery": False,
        "evidenceMode": "historical-replay" if request["case"] == history.CASE else "current",
        "fixture": history.fixture(root) if request["case"] == history.CASE else None,
        "caseMatches": counts,
        **({"rawCaseMatches": raw_counts} if "vendorEvidence" in expected else {}),
        "qualification": report,
    }


def delivery_payload(
    root: Path, qualified: dict[str, Any], evidence_url: str, *, work: Path | None = None
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Recompute qualified update bytes; rebuild cases deliver only a tracked test receipt."""
    if qualified["case"] in UPDATE_CASES:
        if work is None or qualified.get("evidenceMode") != "current":
            raise ValueError("Dependency delivery requires current authenticated update evidence")
        expected, files = validator(root, work)(root, work)
        case_rows(root, expected, qualified["case"])
        report = qualified["qualification"]
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
        if (
            qualified.get("status") != "qualified"
            or report["status"] != "ready"
            or report["source"] != qualified["source"]
            or not hashes
            or hashes != report["files"]
        ):
            raise ValueError("Dependency proposal differs from qualified update files")
        return {**report, "kind": "platform-update-rehearsal", "evidenceMode": "current"}, files
    git(root, "cat-file", "-e", f"{qualified['source']}:{publication.REHEARSAL_PATH}")
    record = {
        "schemaVersion": 1,
        "purpose": "platform-remediation-rehearsal",
        "source": qualified["source"],
        "runId": qualified["runId"],
        "runAttempt": qualified["runAttempt"],
        "case": qualified["case"],
        "qualificationSha256": hashlib.sha256(encode(qualified)).hexdigest(),
        "evidence": evidence_url,
        "productionRecovery": False,
        "evidenceMode": qualified.get("evidenceMode", "current"),
        "fixture": qualified.get("fixture"),
    }
    files = {publication.REHEARSAL_PATH: encode(record)}
    report = {
        "kind": "platform-rehearsal",
        "evidenceMode": qualified.get("evidenceMode", "current"),
        "status": "ready",
        "source": qualified["source"],
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "scans": qualified["qualification"]["scans"],
    }
    return report, files


def lifecycle(
    client: RehearsalGitHub,
    receipt: dict[str, Any],
    report: dict[str, Any],
    files: dict[str, bytes],
    bot: str,
    evidence_url: str,
    work: Path,
) -> dict[str, Any]:
    """Retain the interruption, signed recovery and unchanged refresh as separate observations."""
    progress: dict[str, Any] = {
        "status": "running",
        "branch": client.branch,
        "scope": "rehearsal",
        "bot": bot,
        "source": receipt["source"],
        "payload": "dependency-diff"
        if report.get("kind") == "platform-update-rehearsal"
        else "receipt",
        "files": report["files"],
        "initialHead": publication.branch_head(client),
    }
    output = work / "rehearsal-delivery.json"
    write(output, progress)
    client.interrupt = publication.latest_pull(client) is None
    try:
        result = publication.publish(client, receipt, report, files, bot, evidence_url)
    except InterruptedDelivery:
        head = publication.branch_head(client)
        if head is None or publication.latest_pull(client) is not None:
            raise ValueError("Expected an isolated orphan branch") from None
        ownership = publication.orphan(client, head, bot)
        identity = publication.request(client, f"commits/{head}")
        progress["interruption"] = {
            "head": head,
            "ownership": ownership,
            "identity": {role: identity[role]["login"] for role in ("author", "committer")},
            "signedCommit": publication.request(client, f"git/commits/{head}"),
        }
        write(output, progress)
        refused = []
        for mode in ("unsigned", "altered"):
            try:
                publication.orphan(AlteredCommit(client, head, mode), head, bot)
            except ValueError as error:
                refused.append({"mode": mode, "reason": str(error), "scope": "injected-read"})
            else:
                raise ValueError("Altered ownership was admitted")
        progress["ownershipRefusals"] = refused
        write(output, progress)
        result = publication.publish(client, receipt, report, files, bot, evidence_url)
    if result["status"] not in {"created", "updated", "unchanged"}:
        raise ValueError("Rehearsal PR was dismissed or not delivered")
    progress["delivery"] = result
    head = publication.branch_head(client)
    write(output, progress)
    repeated = publication.publish(client, receipt, report, files, bot, evidence_url)
    if (
        repeated != {"status": "unchanged", "url": result["url"]}
        or publication.branch_head(client) != head
    ):
        raise ValueError("Repeated delivery changed the rehearsal PR or head")
    progress.update(
        status="delivery-passed" if "interruption" in progress else "refresh-passed",
        head=head,
        repeated=repeated,
        ci="not-verified",
    )
    write(output, progress)
    return progress


def main() -> None:
    """Expose the manual workflow's read-only qualification and explicit isolated delivery."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("acquire", "apply", "scan", "qualify", "deliver"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--variant", choices=("baseline", "candidate"))
    parser.add_argument("--bot")
    parser.add_argument("--artifact-id", type=int)
    args = parser.parse_args()
    request = request_record(ROOT)
    try:
        if args.command == "acquire":
            expected = acquire(ROOT, args.work)
            outputs(
                {"source": request["source"], "profiles": json.dumps(expected["affectedProfiles"])}
            )
            return
        if args.command == "apply":
            platform.apply(ROOT, validator(ROOT, args.work)(ROOT, args.work)[1])
            return
        if args.results is None:
            parser.error("qualification requires results")
        if args.command == "scan":
            if args.profile is None or args.variant is None:
                parser.error("scan requires profile and variant")
            platform.scan(
                ROOT,
                args.work,
                args.profile,
                args.variant,
                args.results,
                validator=validator(ROOT, args.work),
            )
            return
        qualified = qualify(ROOT, args.work, args.results)
        if args.command == "qualify":
            write(args.work / "rehearsal-qualified.json", qualified)
            outputs({"delivery_ready": "true"})
        else:
            if os.environ.get("REHEARSAL_DELIVERY") != "true":
                raise ValueError("Manual dispatch did not request rehearsal delivery")
            if args.bot is None or args.artifact_id is None:
                parser.error("delivery requires bot and artifact-id")
            if qualified != read(args.work / "rehearsal-qualified.json"):
                raise ValueError("Rehearsal qualification changed before delivery")
            url = (
                f"https://github.com/{REPOSITORY}/actions/runs/{request['runId']}"
                f"/artifacts/{positive_id(args.artifact_id)}"
            )
            report, files = delivery_payload(ROOT, qualified, url, work=args.work)
            lifecycle(
                RehearsalGitHub(
                    request["runId"], set(files) if request["case"] in UPDATE_CASES else None
                ),
                authority(args.work, request["case"]),
                report,
                files,
                args.bot,
                url,
                args.work,
            )
    except Exception as error:
        write(
            args.work / "rehearsal-failure.json",
            {**request, "status": "failed", "reason": str(error)},
        )
        raise


if __name__ == "__main__":
    main()
