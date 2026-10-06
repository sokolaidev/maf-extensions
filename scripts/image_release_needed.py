"""Reconcile release requests from verified published images and trusted candidate evidence."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from container_release import REPOSITORY, ROOT, read, scan_result, timestamp
from container_release_assets import Evidence, image_tag
from container_release_history import GitHub, History, decode, encode, sha256
from image_security_evidence import verify_inventory
from select_image_security import PROFILES, SCRIPT_INPUTS, affected, metadata_only
from verify_container_release import verify_identity

MARKER = "<!-- image-release-needed:"
STATE = re.compile(r"<!-- image-release-state\n(.*?)\n-->", re.DOTALL)
WORKFLOWS = {
    ".github/workflows/image-security.yml",
    ".github/workflows/container-image-monitor.yml",
    ".github/workflows/container-image-release.yml",
}


def git(root: Path, *args: str) -> str:
    """Read the checked-out source without executing candidate code."""
    return subprocess.check_output(["git", *args], cwd=root).decode("utf-8")


def changed_inputs(root: Path, base: str, head: str, profile: str) -> list[str]:
    """Compare build inputs; publishing and reporting scripts do not change image payloads."""
    for revision in (base, head):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Invalid comparison commit")
    output = git(root, "diff", "--name-only", "--no-renames", "-z", base, head, "--")
    paths = [path for path in output.split("\0") if path]
    ignored = metadata_only(root, base, head, paths)
    result = []
    for path in paths:
        if path in ignored or path.startswith(("tests/", ".github/")):
            continue
        if (
            path.startswith("scripts/")
            and path not in SCRIPT_INPUTS
            and path
            not in {
                "scripts/build_scan_image.sh",
            }
        ):
            continue
        if profile in affected(path):
            result.append(path)
    return sorted(result)


def components(sbom: dict[str, Any], image_id: str) -> list[list[str]]:
    """Ignore catalogue IDs, paths and timestamps when comparing installed components."""
    verify_inventory(sbom, image_id)
    rows = set()
    for artifact in sbom["artifacts"]:
        row = tuple(artifact.get(field, "") for field in ("type", "name", "version", "purl"))
        if any(not isinstance(value, str) for value in row) or not row[1]:
            raise ValueError("Incomplete component identity")
        rows.add(row)
    return [list(row) for row in sorted(rows)]


def baseline(client: GitHub, record: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Require delivered, authenticated completion before using or closing against a release."""
    if record["state"] != "completed" or record["delivery"] != "complete":
        raise ValueError("Release evidence delivery is incomplete")
    evidence = Evidence(client)
    release = evidence.fetch(image_tag(record), directory)
    if (
        release.get("draft") is not False
        or release.get("immutable") is not True
        or client.tag_commit(release["tag_name"]) != record["sourceCommit"]
    ):
        raise ValueError("Release evidence is not immutable at the expected source")
    candidate = record["candidate"]
    verify_identity(candidate, directory, bundles=True)
    if sha256((directory / "evidence-index.json").read_bytes()) != record["evidenceIndexSha256"]:
        raise ValueError("Evidence index differs from the current catalogue")
    assessment = read(directory / "assessment.json")
    verified = scan_result(
        read(directory / "grype.json"), record["imageId"], assessment["assessedAt"]
    )
    if verified != assessment or verified["outcome"] != "clean":
        raise ValueError("Published assessment does not satisfy the release gate")
    return {
        "components": components(read(directory / "sbom.syft.json"), record["imageId"]),
        "evidence": f"https://github.com/{REPOSITORY}/releases/tag/{release['tag_name']}",
    }


def run_artifacts(client: GitHub, run_id: str) -> list[dict[str, Any]]:
    """Require the complete, bounded scan artifact listing."""
    listing = decode(
        client.request(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100")
    )
    if listing["total_count"] != len(listing["artifacts"]):
        raise ValueError("Incomplete candidate artifact listing")
    return listing["artifacts"]


def action_listing(client: GitHub, endpoint: str, field: str) -> list[dict[str, Any]]:
    """Require complete Actions metadata without losing entries to pagination changes."""
    items = {}
    total = None
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 10_001):
        listing = decode(client.request(f"{endpoint}{separator}per_page=100&page={page}"))
        if total is None:
            total = listing["total_count"]
        if listing["total_count"] != total:
            raise ValueError("Scan history changed during retrieval")
        batch = listing[field]
        for item in batch:
            if item["id"] in items:
                raise ValueError("Duplicate scan history entry")
            items[item["id"]] = item
        if len(batch) < 100:
            break
    else:
        raise ValueError("Scan history pagination exceeded its bound")
    if len(items) != total:
        raise ValueError("Incomplete scan history")
    return list(items.values())


def newer_profiles(client: GitHub, run: dict[str, Any]) -> set[str]:
    """Reject superseded scan events even when no issue exists to retain their ordering."""
    # Unfiltered listing avoids GitHub's 1,000-result cap on run searches.
    runs = action_listing(
        client, f"repos/{REPOSITORY}/actions/workflows/image-security.yml/runs", "workflow_runs"
    )
    if run["id"] not in {item["id"] for item in runs}:
        raise ValueError("Incomplete scan history")
    result = set()
    for other in runs:
        if (
            other["id"] == run["id"]
            or other.get("head_repository", {}).get("full_name") != REPOSITORY
            or other.get("head_branch") != "main"
            or other.get("event") not in {"schedule", "workflow_dispatch"}
            or other.get("path") != ".github/workflows/image-security.yml"
            or other.get("status") != "completed"
            or timestamp(other["updated_at"]) < timestamp(run["updated_at"])
        ):
            continue
        jobs = action_listing(
            client, f"repos/{REPOSITORY}/actions/runs/{other['id']}/jobs?filter=all", "jobs"
        )
        if not jobs:
            raise ValueError("Completed scan has no job history")
        names = {job["name"] for job in jobs}
        result.update(profile for profile in PROFILES if f"Image security ({profile})" in names)
    return result


def candidates(client: GitHub, run_id: str, directory: Path) -> dict[str, Any]:
    """Read artifacts only from completed main-branch scans in this repository."""
    if not run_id:
        return {}
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Invalid triggering run")
    run = decode(client.request(f"repos/{REPOSITORY}/actions/runs/{run_id}"))
    if (
        run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("head_branch") != "main"
        or run.get("event") not in {"schedule", "workflow_dispatch", "workflow_run"}
        or run.get("path") not in WORKFLOWS
    ):
        raise ValueError("Untrusted triggering workflow")
    if run["status"] != "completed" or run["path"] != ".github/workflows/image-security.yml":
        return {}
    result = {}
    artifacts = run_artifacts(client, run_id)
    superseded = newer_profiles(client, run)
    for profile in PROFILES:
        if profile in superseded:
            continue
        matches = [a for a in artifacts if a["name"] == f"image-security-{profile}"]
        if not matches:
            continue
        if len(matches) != 1 or matches[0]["expired"]:
            raise ValueError("Candidate artifact is ambiguous or expired")
        target = directory / profile
        subprocess.run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                REPOSITORY,
                "--name",
                matches[0]["name"],
                "--dir",
                str(target),
            ],
            check=True,
            timeout=600,
        )
        try:
            build = read(target / "build.json")
            if build["profile"] != profile or build["source_commit"] != run["head_sha"]:
                raise ValueError("Candidate source does not match the scan run")
            inventory = components(read(target / "sbom.syft.json"), build["local_image_id"])
            assessment = scan_result(
                read(target / "grype.json"), build["local_image_id"], run["updated_at"]
            )
            result[profile] = {
                "components": inventory,
                "assessment": assessment,
                "source": run["head_sha"],
                "observedAt": run["updated_at"],
                "evidence": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{run['run_attempt']}",
            }
        except (OSError, ValueError, KeyError, TypeError) as error:
            if run.get("conclusion") == "success":
                raise ValueError(
                    "Successful scan did not deliver valid candidate evidence"
                ) from error
            # A failed scan is handled by its workflow's operational failure tracker.
            print(f"{profile}: candidate evidence unavailable: {error}")
    return result


def desired(
    record: dict[str, Any],
    released: dict[str, Any],
    changed: list[str],
    source: str,
    candidate: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep unresolved reasons until a replacement release demonstrably addresses them."""
    state = (
        copy.deepcopy(previous)
        if previous
        else {
            "baseline": record["registryDigest"],
            "reasons": {},
            "status": "Awaiting release preparation",
        }
    )
    reasons = state["reasons"]
    if candidate and timestamp(candidate["observedAt"]) <= max(
        timestamp(record["completedAt"]),
        timestamp(state.get("candidateObservedAt", record["completedAt"])),
    ):
        candidate = None
    replacement = state["baseline"] != record["registryDigest"]
    new_release = state.get("currentDigest", state["baseline"]) != record["registryDigest"]
    inventory_hash = sha256(encode(released["components"]))
    if changed:
        reasons["source"] = {"commit": source, "paths": changed}
    elif replacement:
        reasons.pop("source", None)
    pending_components = reasons.get("components")
    if (
        replacement
        and pending_components
        and pending_components["inventorySha256"] == inventory_hash
    ):
        reasons.pop("components", None)
    observation = record.get("latestAttempt", {})
    vulnerable = (
        observation
        if observation.get("outcome") == "vulnerable"
        else (
            record.get("lastKnownVulnerable", {}) if observation.get("outcome") != "clean" else {}
        )
    )
    if vulnerable:
        reasons["vulnerabilities"] = {
            "findings": sorted(
                vulnerable["findings"], key=lambda item: json.dumps(item, sort_keys=True)
            ),
            "evidence": f"https://github.com/{REPOSITORY}/releases/tag/{vulnerable['evidenceRelease']}",
        }
    elif replacement:
        reasons.pop("vulnerabilities", None)
    if candidate:
        target_hash = sha256(encode(candidate["components"]))
        if target_hash != inventory_hash:
            old = {tuple(row) for row in released["components"]}
            new = {tuple(row) for row in candidate["components"]}
            reasons["components"] = {
                "inventorySha256": target_hash,
                "added": [list(row) for row in sorted(new - old)],
                "removed": [list(row) for row in sorted(old - new)],
            }
        elif replacement:
            reasons.pop("components", None)
        state["status"] = (
            "Last candidate scan passed; release gate still required"
            if candidate["assessment"]["outcome"] == "clean"
            else "Blocked on candidate remediation"
        )
        state["candidateEvidence"] = candidate["evidence"]
        state["candidateObservedAt"] = candidate["observedAt"]
        state["candidateFindings"] = sorted(
            candidate["assessment"]["findings"], key=lambda item: json.dumps(item, sort_keys=True)
        )
    elif not previous or new_release:
        state["status"] = "Awaiting release preparation"
        state.pop("candidateEvidence", None)
        state.pop("candidateFindings", None)
    if "vulnerabilities" in reasons and not state.get("candidateEvidence"):
        state["status"] = "Blocked on remediation; no passing replacement assessed"
    if not reasons and not previous:
        return None
    state["currentVersion"] = record["version"]
    state["currentDigest"] = record["registryDigest"]
    state["releaseEvidence"] = released["evidence"]
    state["resolved"] = replacement and not reasons
    return state


def stable(state: dict[str, Any]) -> dict[str, Any]:
    """Evidence URLs may advance without changing the actionable request."""
    result = copy.deepcopy(state)
    result.pop("candidateEvidence", None)
    for reason in result["reasons"].values():
        reason.pop("evidence", None)
    return result


def body(profile: str, state: dict[str, Any]) -> str:
    """Keep machine-owned request state reviewable and bounded by GitHub's issue limit."""
    reasons = state["reasons"]
    details = []
    if "source" in reasons:
        source = reasons["source"]
        details.append(
            f"Build inputs changed at `{source['commit']}`: "
            + ", ".join(f"`{p}`" for p in source["paths"])
        )
    if "components" in reasons:
        for change in ("added", "removed"):
            rows = reasons["components"][change]
            details.append(
                f"Components {change} ({len(rows)}): "
                + ", ".join(f"`{r[1]} {r[2]}`" for r in rows[:40])
            )
    if "vulnerabilities" in reasons:
        value = reasons["vulnerabilities"]
        details.append(
            "High/Critical findings (including unfixed): "
            + ", ".join(
                f"`{f['id']}` ({f['severity']}, {f['fix']['state']})" for f in value["findings"]
            )
        )
        details.append(f"[Monitoring evidence]({value['evidence']}).")
    if state.get("candidateFindings"):
        details.append(
            "Candidate blockers: "
            + ", ".join(
                f"`{f['id']}` ({f['severity']}, {f['fix']['state']})"
                for f in state["candidateFindings"]
            )
        )
    state_json = (
        json.dumps(state, sort_keys=True, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    text = (
        f"{MARKER}{profile} -->\n<!-- image-release-state\n{state_json}\n-->\n\n"
        "**Is your feature request related to a problem? Please describe.**\n\n"
        f"Profile: `{profile}`. Current release: `{state['currentVersion']}` at `{state['currentDigest']}`.\n\n"
        + "\n\n".join(details)
        + "\n\n**Describe the solution you'd like**\n\n"
        + (
            "Resolved by the completed, verified replacement release."
            if state["resolved"]
            else f"Prepare a replacement image release. Status: **{state['status']}**."
        )
        + "\n\nPublication requires the existing exact-candidate runtime, SBOM, High/Critical scan, approval and signature checks. A passing daily scan does not close this request.\n\n"
        "**Describe alternatives you've considered**\n\n"
        "Do not publish automatically or treat a scanner outage as evidence that a new image is needed.\n\n"
        "**Additional context**\n\n"
        f"[Verified release evidence]({state['releaseEvidence']}). "
        + (
            f"[Candidate evidence]({state['candidateEvidence']}). "
            if state.get("candidateEvidence")
            else ""
        )
        + "This body is maintained automatically; add maintainer notes as comments. Newer observations advance saved ordering metadata without reminder comments.\n"
    )
    if len(text) > 60_000:
        raise ValueError("Release request exceeds the issue size limit")
    return text


def reconcile(
    client: GitHub, profile: str, state: dict[str, Any] | None, issue: dict[str, Any] | None
) -> None:
    """Use one issue mutation per change so retries do not duplicate notifications."""
    if state is None:
        return
    endpoint = f"repos/{REPOSITORY}/issues"
    if issue:
        match = STATE.search(issue["body"])
        if match is None:
            raise ValueError("Release request has no recoverable state")
        old = decode(match[1])
        if stable(old) == stable(state) and issue["state"] == (
            "closed" if state["resolved"] else "open"
        ):
            return
        payload = {"body": body(profile, state), "state": "closed" if state["resolved"] else "open"}
        if state["resolved"]:
            payload["state_reason"] = "completed"
        client.request(f"{endpoint}/{issue['number']}", method="PATCH", payload=encode(payload))
    else:
        client.request(
            endpoint,
            method="POST",
            payload=encode(
                {
                    "title": f"Image release needed: {profile}",
                    "body": body(profile, state),
                }
            ),
        )


def main() -> None:
    """Only the trusted main workflow may write release-needed issues."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_EVENT_NAME") not in {"workflow_run", "workflow_dispatch"}
    ):
        raise ValueError("Issue reconciliation requires the trusted main workflow")
    client = GitHub()
    history = History(client)
    snapshot = history.head()
    if snapshot is None:
        print("No completed image releases; no replacement requests to reconcile.")
        return
    issues = client.pages(f"repos/{REPOSITORY}/issues?state=all")
    source = git(ROOT, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory(prefix="maf-release-requests-") as temporary:
        root = Path(temporary)
        observed = candidates(client, os.environ.get("TRIGGER_RUN_ID", ""), root / "candidates")
        plans = []
        for profile, name in snapshot.catalogue["current"].items():
            matches = [
                i
                for i in issues
                if "pull_request" not in i
                and i.get("user", {}).get("login") == "github-actions[bot]"
                and i.get("user", {}).get("type") == "Bot"
                and f"{MARKER}{profile} -->" in (i.get("body") or "")
            ]
            active = [i for i in matches if i["state"] == "open"]
            if len(active) > 1:
                raise ValueError("Multiple open release requests for one profile")
            issue = (
                active[0]
                if active
                else (max(matches, key=lambda i: i["number"]) if matches else None)
            )
            previous = None
            if issue:
                match = STATE.search(issue["body"])
                if match is None:
                    raise ValueError("Release request has no recoverable state")
                previous = decode(match[1])
                if previous["resolved"]:
                    previous = None
            record = snapshot.catalogue["releases"][name]
            released = baseline(client, record, root / profile)
            changed = changed_inputs(ROOT, record["sourceCommit"], source, profile)
            candidate = observed.get(profile)
            if candidate and changed_inputs(ROOT, candidate["source"], source, profile):
                candidate = None
            required_source = (
                git(ROOT, "log", "-1", "--format=%H", source, "--", *changed).strip()
                if changed
                else source
            )
            state = desired(record, released, changed, required_source, candidate, previous)
            if state is not None:
                body(profile, state)
            plans.append((profile, state, issue))
        latest = history.head()
        if latest is None or latest.reference != snapshot.reference:
            raise ValueError("Release history changed during reconciliation")
        for profile, state, issue in plans:
            if args.dry_run:
                print(json.dumps({"profile": profile, "state": state}, sort_keys=True))
            else:
                reconcile(client, profile, state, issue)


if __name__ == "__main__":
    main()
