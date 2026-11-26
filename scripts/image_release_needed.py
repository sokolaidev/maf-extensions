"""Reconcile release requests from verified published images and trusted candidate evidence."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import image_security_issue as image_issue
from container_release import (
    REPOSITORY,
    ROOT,
    read,
    release_scan_passes,
    scan_result,
    timestamp,
    version,
)
from container_release_assets import Evidence, image_tag
from container_release_history import GitHub, History, decode, encode, sha256
from image_scan_policy import BICEP_ROUTES, evidence_route
from image_security_evidence import verify_inventory
from report_stuck_releases import as_code
from select_image_security import (
    PROFILES,
    SCRIPT_INPUTS,
    WHEEL_PACKAGES,
    changed_profiles,
    metadata_only,
)
from verify_container_release import verify_identity

MARKER = "<!-- image-release-needed:"
STATE = re.compile(r"<!-- image-release-state\n(.*?)\n-->", re.DOTALL)
WORKFLOWS = {
    ".github/workflows/image-security.yml",
    ".github/workflows/container-image-monitor.yml",
    ".github/workflows/container-image-release.yml",
}
WORKSPACE_INPUTS = {"uv.lock", "uv.toml", "pyproject.toml", ".python-version", ".gitignore"}


def bicep_diagnostics(source: str) -> str:
    """Normalize only side-effect-free route rejection messages and equivalent OR guards."""
    tree = ast.parse(source)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "validate_route"
    ]
    if len(functions) != 1:
        raise ValueError("Unknown route validator")
    function = functions[0]
    guards = []
    for node in function.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1:
            raise ValueError("Unknown route guard")
        failure = node.body[0]
        if not isinstance(failure, ast.Raise) or failure.cause is not None:
            raise ValueError("Unknown route rejection")
        call = failure.exc
        if (
            not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Name)
            or call.func.id != "ValueError"
            or len(call.args) != 1
            or call.keywords
        ):
            raise ValueError("Unknown route error")
        message = call.args[0]
        if not isinstance(message, (ast.Constant, ast.JoinedStr)) or any(
            not isinstance(
                part, (ast.Constant, ast.JoinedStr, ast.FormattedValue, ast.Name, ast.Load)
            )
            for part in ast.walk(message)
        ):
            raise ValueError("Route error may have side effects")
        guards.extend(
            node.test.values
            if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or)
            else [node.test]
        )
    if not guards:
        raise ValueError("Missing route guards")
    function.body = [ast.Expr(value=guard) for guard in guards]
    return ast.dump(tree)


def required(state: dict[str, Any]) -> bool:
    """Only security and declared package requirements justify replacement requests."""
    return bool({"vulnerabilities", "route", "requirements"} & state["reasons"].keys())


def package_requirements(root: Path) -> dict[str, list[dict[str, str]]]:
    """Read reviewed minimum image versions required by package consumers."""
    policy = read(root / "images/package-requirements.json")
    if (
        not isinstance(policy, dict)
        or set(policy) != {"schemaVersion", "profiles"}
        or policy["schemaVersion"] != 1
        or not isinstance(policy["profiles"], dict)
    ):
        raise ValueError("Invalid package image requirements")
    for profile, entries in policy["profiles"].items():
        if profile not in PROFILES or not isinstance(entries, list) or not entries:
            raise ValueError("Invalid package image requirement profile")
        packages = set()
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"package", "minimumVersion", "reason"}
                or any(not isinstance(value, str) or not value.strip() for value in entry.values())
                or not re.fullmatch(r"maf-[a-z0-9]+(?:-[a-z0-9]+)*", entry["package"])
                or not (root / "packages" / entry["package"] / "pyproject.toml").is_file()
                or entry["package"] in packages
            ):
                raise ValueError("Invalid package image requirement")
            version(entry["minimumVersion"])
            packages.add(entry["package"])
    return policy["profiles"]


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
    try:
        ignored = metadata_only(root, base, head, paths)
    except (
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        ignored = set()
    result = []
    for path in paths:
        if path in ignored or path.startswith(("tests/", ".github/")):
            continue
        if path == "images/README.md":
            continue
        if path in WORKSPACE_INPUTS and profile != "hyperlight-aks-probe":
            continue
        if re.fullmatch(r"packages/[^/]+/pyproject\.toml", path) and (
            profile != "hyperlight-aks-probe" or path.split("/")[1] not in WHEEL_PACKAGES
        ):
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
        if path == "scripts/build_bicep_image.py":
            try:
                before, after = (
                    bicep_diagnostics(git(root, "show", f"{ref}:{path}")) for ref in (base, head)
                )
                if before == after:
                    continue
            except (subprocess.CalledProcessError, SyntaxError, ValueError):
                # Unproven equivalence must retain the build input.
                pass
        if profile in changed_profiles(root, base, head, path):
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


def release_evidence(client: GitHub, record: dict[str, Any], directory: Path) -> str:
    """Authenticate a delivered image release independently of its vulnerability assessment."""
    if record["state"] != "completed" or record["delivery"] != "complete":
        raise ValueError(
            "Release evidence delivery is incomplete: "
            f"{record['profile']}/{record['version']} "
            f"(state={record['state']}, delivery={record['delivery']})"
        )
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
    return f"https://github.com/{REPOSITORY}/releases/tag/{release['tag_name']}"


def baseline(client: GitHub, record: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Require authenticated completion and a release-eligible original scan for image refreshes."""
    evidence_url = release_evidence(client, record, directory)
    candidate = record["candidate"]
    assessment = read(directory / "assessment.json")
    verified = scan_result(
        read(directory / "grype.json"),
        record["imageId"],
        assessment["assessedAt"],
        sbom=read(directory / "sbom.syft.json"),
    )
    if verified != assessment or not release_scan_passes(verified):
        raise ValueError("Published assessment does not satisfy the release gate")
    result = {
        "components": components(read(directory / "sbom.syft.json"), record["imageId"]),
        "evidence": evidence_url,
        "assessment": verified,
    }
    if record["profile"] in BICEP_ROUTES:
        route = candidate.get("bicepRoute", "upstream")
        if route not in {"source", "upstream"}:
            raise ValueError("Published Bicep route is invalid")
        runtime = read(directory / "runtime.json")
        build = runtime.get("bicepAvm", runtime.get("bicep", {})).get("build")
        if route == "source" or build is not None:
            if (
                evidence_route(
                    record["profile"],
                    record["imageId"],
                    runtime,
                    read(directory / "sbom.syft.json"),
                )
                != route
            ):
                raise ValueError(
                    "Published Bicep route differs from authenticated runtime evidence"
                )
        result["bicepRoute"] = route
    return result


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


def newer_profiles(client: GitHub, run: dict[str, Any], observed_at: dict[str, str]) -> set[str]:
    """Reject superseded scan events even when no issue exists to retain their ordering."""
    if not observed_at:
        return set()
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
            or timestamp(other["updated_at"]) < min(timestamp(at) for at in observed_at.values())
        ):
            continue
        jobs = action_listing(
            client, f"repos/{REPOSITORY}/actions/runs/{other['id']}/jobs?filter=all", "jobs"
        )
        if not jobs:
            raise ValueError("Completed scan has no job history")
        bicep = tuple(
            profile
            for profile in observed_at
            if profile in BICEP_ROUTES
            and any(
                job["name"] == f"Image security ({profile})"
                and timestamp(job["completed_at"]) >= timestamp(observed_at[profile])
                for job in jobs
            )
        )
        diagnostics = set()
        if bicep:
            with tempfile.TemporaryDirectory(prefix="maf-scan-ordering-") as temporary:
                evidence = candidates(
                    client,
                    str(other["id"]),
                    Path(temporary),
                    historical=True,
                    profiles=bicep,
                    route_only=True,
                )
            diagnostics = {
                profile
                for profile, value in evidence.items()
                if value.get("bicepRoute") != BICEP_ROUTES[profile]
            }
        for job in jobs:
            for profile, at in observed_at.items():
                if (
                    profile not in diagnostics
                    and job["name"] == f"Image security ({profile})"
                    and timestamp(job["completed_at"]) >= timestamp(at)
                ):
                    result.add(profile)
    return result


def candidates(
    client: GitHub,
    run_id: str,
    directory: Path,
    *,
    historical: bool = False,
    profiles: tuple[str, ...] = PROFILES,
    route_only: bool = False,
) -> dict[str, Any]:
    """Read artifacts only from completed main-branch scans in this repository."""
    if route_only and (not historical or not set(profiles) <= BICEP_ROUTES.keys()):
        raise ValueError("Route-only evidence is restricted to historical Bicep ordering")
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
    jobs = action_listing(
        client, f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=all", "jobs"
    )
    for profile in profiles:
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
            if build["run_id"] != run_id or not re.fullmatch(r"[1-9][0-9]*", build["run_attempt"]):
                raise ValueError("Candidate does not identify its producing attempt")
            profile_jobs = [job for job in jobs if job["name"] == f"Image security ({profile})"]
            producers = [
                job for job in profile_jobs if str(job["run_attempt"]) == build["run_attempt"]
            ]
            if len(producers) != 1:
                raise ValueError("Candidate has no unique producing job")
            producer = producers[0]
            if producer["status"] != "completed" or str(producer["run_id"]) != run_id:
                raise ValueError("Candidate producing job is incomplete or unrelated")
            started, completed = (
                timestamp(producer["started_at"]),
                timestamp(producer["completed_at"]),
            )
            if not all(
                started <= timestamp(at) <= completed
                for at in (build["collected_at"], matches[0]["created_at"])
            ):
                raise ValueError("Candidate evidence was not collected during its producing job")
            if any(job["run_attempt"] > producer["run_attempt"] for job in profile_jobs):
                continue
            sbom = read(target / "sbom.syft.json")
            inventory = components(sbom, build["local_image_id"])
            result[profile] = {
                "components": inventory,
                "source": run["head_sha"],
                "observedAt": producer["completed_at"],
                "evidence": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{producer['run_attempt']}",
            }
            if profile in BICEP_ROUTES:
                result[profile]["bicepRoute"] = evidence_route(
                    profile, build["local_image_id"], read(target / "runtime.json"), sbom
                )
            if not route_only:
                result[profile]["assessment"] = scan_result(
                    read(target / "grype.json"),
                    build["local_image_id"],
                    producer["completed_at"],
                    sbom=sbom,
                )
        except (OSError, ValueError, KeyError, TypeError) as error:
            result.pop(profile, None)
            if run.get("conclusion") == "success":
                raise ValueError(
                    "Successful scan did not deliver valid candidate evidence"
                ) from error
            # A failed scan is handled by its workflow's operational failure tracker.
            print(f"{profile}: candidate evidence unavailable: {error}")
    if historical:
        return result
    result = {
        profile: value
        for profile, value in result.items()
        if profile not in BICEP_ROUTES or value["bicepRoute"] == BICEP_ROUTES[profile]
    }
    superseded = newer_profiles(
        client, run, {profile: value["observedAt"] for profile, value in result.items()}
    )
    return {profile: value for profile, value in result.items() if profile not in superseded}


def desired(
    record: dict[str, Any],
    released: dict[str, Any],
    changed: list[str],
    source: str,
    candidate: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    requirements: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Request security remediation or an unmet declared package image minimum."""
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
    reasons.pop("components", None)
    reasons.pop("source", None)
    reasons.pop("requirements", None)
    unmet = [
        entry
        for entry in requirements or []
        if version(record["version"]) < version(entry["minimumVersion"])
    ]
    if unmet:
        reasons["requirements"] = {"packages": unmet}
    if changed:
        state["changedInputs"] = {"commit": source, "paths": changed}
    else:
        state.pop("changedInputs", None)
    expected_route = BICEP_ROUTES.get(record["profile"])
    if expected_route:
        if released["bicepRoute"] != expected_route:
            reasons["route"] = {"published": released["bicepRoute"], "replacement": expected_route}
        else:
            reasons.pop("route", None)
        if candidate and candidate.get("bicepRoute") != expected_route:
            candidate = None
    if candidate and timestamp(candidate["observedAt"]) <= max(
        timestamp(record["completedAt"]),
        timestamp(state.get("candidateObservedAt", record["completedAt"])),
    ):
        candidate = None
    new_release = state.get("currentDigest", state["baseline"]) != record["registryDigest"]
    observation = record.get("latestAttempt", {})
    pending_vulnerabilities = reasons.get("vulnerabilities")
    if observation.get("outcome") not in {"clean", "vulnerable"}:
        observation = record.get("lastKnownVulnerable", {})
    release_assessment = released.get("assessment")
    evidence = released["evidence"]
    if release_assessment and (
        not observation
        or timestamp(observation["assessedAt"]) <= timestamp(release_assessment["assessedAt"])
    ):
        observation = release_assessment
    elif observation.get("outcome") == "vulnerable":
        evidence = f"https://github.com/{REPOSITORY}/releases/tag/{observation['evidenceRelease']}"
    if observation.get("outcome") == "vulnerable":
        reasons["vulnerabilities"] = {
            "baselineDigest": record["registryDigest"],
            "findings": sorted(
                observation["findings"], key=lambda item: json.dumps(item, sort_keys=True)
            ),
            "evidence": evidence,
        }
    elif (
        pending_vulnerabilities
        and pending_vulnerabilities.get(
            "baselineDigest", state.get("currentDigest", state["baseline"])
        )
        != record["registryDigest"]
    ):
        reasons.pop("vulnerabilities", None)
    if candidate:
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
    state["resolved"] = not reasons
    if state.get("candidateEvidence"):
        state["status"] = (
            "Blocked on candidate remediation"
            if state.get("candidateFindings")
            else "Last candidate scan passed; release gate still required"
        )
    elif "vulnerabilities" not in reasons:
        state["status"] = "Awaiting release preparation"
    withdrawn_minimum = any(
        version(record["version"]) < version(entry["minimumVersion"])
        for entry in (previous or {}).get("reasons", {}).get("requirements", {}).get("packages", [])
    )
    if state["resolved"] and (
        not new_release or not previous or not required(previous) or withdrawn_minimum
    ):
        state["resolution"] = "policy-reconciled"
    else:
        state.pop("resolution", None)
    return resolved_state(state) if state["resolved"] else state


def resolved_state(state: dict[str, Any]) -> dict[str, Any]:
    """Normalize closed-request presentation without changing its resolution evidence."""
    result = state | {
        "status": (
            "No security or package requirement needs replacement"
            if state.get("resolution") == "policy-reconciled"
            else "Incompatible candidate route excluded; no replacement required"
            if state.get("resolution") == "route-reconciled"
            else "No relevant build-input differences; no replacement required"
            if state.get("resolution") == "source-reconciled"
            else "Resolved by the completed, verified replacement release"
        )
    }
    result.pop("candidateEvidence", None)
    result.pop("candidateFindings", None)
    return result


def body(profile: str, state: dict[str, Any]) -> str:
    """Keep machine-owned request state reviewable and bounded by GitHub's issue limit."""
    reasons = state["reasons"]
    details = []
    if "route" in reasons:
        route = reasons["route"]
        details.append(
            f"Bicep replacement route policy: {as_code(route['replacement'])}; "
            f"published route: {as_code(route['published'])}."
        )
    if "requirements" in reasons:
        for entry in reasons["requirements"]["packages"]:
            details.append(
                f"Package {as_code(entry['package'])} requires image version "
                f"{as_code(entry['minimumVersion'])} or newer: {as_code(entry['reason'])}."
            )
    if source := state.get("changedInputs"):
        details.append(
            f"Build inputs changed at {as_code(source['commit'])}: "
            + ", ".join(as_code(p) for p in source["paths"])
            + ". These differences alone do not require replacement."
        )
    if "vulnerabilities" in reasons:
        value = reasons["vulnerabilities"]
        details.append(
            "High/Critical findings (including unfixed): "
            + ", ".join(
                f"{as_code(f['id'])} ({as_code(f['severity'])}, {as_code(f['fix']['state'])})"
                for f in value["findings"]
            )
        )
        details.append(f"[Monitoring evidence]({value['evidence']}).")
    if state.get("candidateFindings"):
        details.append(
            "Candidate blockers: "
            + ", ".join(
                f"{as_code(f['id'])} ({as_code(f['severity'])}, {as_code(f['fix']['state'])})"
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
        f"Profile: {as_code(profile)}. Current release: {as_code(state['currentVersion'])} at {as_code(state['currentDigest'])}.\n\n"
        + "\n\n".join(details)
        + "\n\n**Describe the solution you'd like**\n\n"
        + (
            "No known published-image security finding or declared package requirement needs a replacement. Routine source and component drift does not request a release. No replacement release is claimed."
            if state.get("resolution") == "policy-reconciled"
            else "Verified evidence excluded an incompatible diagnostic candidate. No replacement release is required."
            if state.get("resolution") == "route-reconciled"
            else "Verified source comparison found no relevant build-input differences. No replacement release is required."
            if state.get("resolution") == "source-reconciled"
            else (
                "Resolved by the completed, verified replacement release."
                if state["resolved"]
                else f"Prepare a replacement image release. Status: **{state['status']}**."
            )
        )
        + "\n\nPublication requires the existing exact-candidate runtime, SBOM, Critical scan, approval and signature checks. High findings remain monitoring warnings and remediation requests. A passing daily scan does not close this request.\n\n"
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
    client: GitHub,
    profile: str,
    state: dict[str, Any] | None,
    issue: dict[str, Any] | None,
    related: list[dict] | None = None,
) -> None:
    """Use one issue mutation per change so retries do not duplicate notifications."""
    if state is None or (issue is None and state["resolved"]):
        return
    if not state["resolved"] and not required(state):
        raise ValueError("Release request has no security or package requirement")
    endpoint = f"repos/{REPOSITORY}/issues"
    if issue:
        match = STATE.search(issue["body"])
        if match is None and not image_issue.matches([issue], profile):
            raise ValueError("Release request has no recoverable state")
        if match:
            old = decode(match[1])
            if not isinstance(old, dict):
                raise ValueError("Release request has no recoverable state")
    signals = image_issue.release_signals(state)
    payload = image_issue.update(
        issue,
        profile,
        "release",
        body(profile, state),
        signals,
        active=not state["resolved"],
        related=related,
    )
    if (
        payload
        and payload.get("state") == "closed"
        and state.get("resolution")
        in {"source-reconciled", "route-reconciled", "policy-reconciled"}
    ):
        payload["state_reason"] = "not_planned"
    if issue:
        message = image_issue.notice(issue, signals, lane="release")
        if message:
            comment_endpoint = f"{endpoint}/{issue['number']}/comments"
            if not image_issue.already_notified(client.pages(comment_endpoint), message):
                client.request(comment_endpoint, method="POST", payload=encode({"body": message}))
        if payload is not None:
            client.request(f"{endpoint}/{issue['number']}", method="PATCH", payload=encode(payload))
        for previous in related or []:
            retired = image_issue.retirement(previous, issue, profile)
            if retired:
                client.request(
                    f"{endpoint}/{previous['number']}", method="PATCH", payload=encode(retired)
                )
    elif payload is not None:
        client.request(endpoint, method="POST", payload=encode(payload))


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
    requirements = package_requirements(ROOT)
    client = GitHub()
    history = History(client)
    snapshot = history.head()
    if snapshot is None:
        print("No completed image releases; no replacement requests to reconcile.")
        return
    issues = client.pages(f"repos/{REPOSITORY}/issues?state=all&sort=created&direction=asc")
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
            issue = image_issue.select(issues, profile)
            release_issue = (
                issue
                if issue and STATE.search(issue.get("body") or "")
                else max(matches, key=lambda i: i["number"], default=None)
            )
            previous = None
            resolved_previous = None
            if release_issue:
                match = STATE.search(release_issue["body"])
                if match is None:
                    raise ValueError("Release request has no recoverable state")
                previous = decode(match[1])
                if previous["resolved"]:
                    resolved_previous = previous
                    previous = None
            record = snapshot.catalogue["releases"][name]
            released = baseline(client, record, root / profile)
            changed = changed_inputs(ROOT, record["sourceCommit"], source, profile)
            candidate = observed.get(profile)
            if candidate and changed_inputs(ROOT, candidate["source"], source, profile):
                candidate = None
            required_source = (
                git(
                    ROOT, "--literal-pathspecs", "log", "-1", "--format=%H", source, "--", *changed
                ).strip()
                if changed
                else source
            )
            state = desired(
                record,
                released,
                changed,
                required_source,
                candidate,
                previous,
                requirements.get(profile),
            )
            if state is None and resolved_previous is not None:
                state = resolved_state(resolved_previous)
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
                reconcile(client, profile, state, issue, image_issue.matches(issues, profile))


if __name__ == "__main__":
    main()
