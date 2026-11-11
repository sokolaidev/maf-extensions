"""Qualify Bicep and vendor-package fixes without changing published-image recovery state."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import vendor_fix_evidence
from bicep_source import policy
from bicep_source import verify_inventory as verify_bicep_inventory
from check_container_release_image import check
from container_release import REPOSITORY, ROOT, read, scan_result, write
from container_release_history import GitHub, HistoryAdvanced, positive_id
from image_release_needed import git
from image_remediation import acquisition_summary, outputs, remaining, verify_engine
from image_remediation_evidence import EvidenceChanging, current, deferred, require_image_scope
from image_remediation_evidence import acquire_with_retry as acquire
from image_remediation_pr import BotGitHub, publish
from image_scan_policy import BICEP_ROUTES, evidence_route, scan_route
from image_security_evidence import PROVIDER_MANIFESTS, record, verify_inventory, verify_providers
from plan_go_remediation import CONTEXT, RECIPE, digest, locations
from plan_platform_remediation import (
    BICEP,
    RUNTIME_RECIPES,
    acquire_plan,
    apk_inventory,
    file_hashes,
    inputs,
    plan,
    proposal,
    version,
)
from prepare_container_release import assess
from select_image_security import PROFILES

ACTIONABLE = {"update", "source-pin-sufficient", "rebuild-required"}


class PlatformGitHub(BotGitHub):
    """Keep platform proposals separate from the existing coordinated Go fix."""

    branch = "bot/platform-image-remediation"
    title = "build: remediate Bicep and OS image dependencies with verified fixes"

    def __init__(self, paths: set[str]) -> None:
        self.paths = paths

    def request(self, endpoint: str, **kwargs: Any) -> bytes:
        """Repeat competing-PR discovery immediately before branch and PR writes."""
        if kwargs.get("method", "GET") != "GET" and (endpoint == "graphql" or "/pulls" in endpoint):
            conflicts = competing_pulls(self, self.paths)
            if conflicts:
                raise ValueError(f"Competing source PR appeared before delivery: {conflicts}")
        return super().request(endpoint, **kwargs)


def competing_pulls(client: GitHub, paths: set[str]) -> list[int]:
    """Defer to existing source proposals, including Dependabot's native manifest updates."""
    conflicts = []
    for pull in client.pages(f"repos/{REPOSITORY}/pulls?state=open&base=main"):
        if (
            pull["head"]["ref"] == PlatformGitHub.branch
            and pull["head"]["repo"]["full_name"] == REPOSITORY
        ):
            continue
        number = positive_id(pull["number"])
        files = client.pages(f"repos/{REPOSITORY}/pulls/{number}/files")
        if paths & {item["filename"] for item in files}:
            conflicts.append(number)
    return sorted(conflicts)


def validate(root: Path, work: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Recompute every edit from retained evidence before consuming a proposal."""
    receipt = read(work / "receipt.json")
    if "rehearsalFixture" in receipt:
        raise ValueError("Historical evidence requires the isolated rehearsal entry point")
    if git(root, "rev-parse", "HEAD").strip() != receipt["source"]:
        raise ValueError("Platform qualification source changed")
    expected = read(work / "plan.json")
    require_image_scope(receipt, expected["affectedProfiles"])
    directories = [work / "evidence" / name for name in receipt["directories"]]
    vendor = (
        vendor_fix_evidence.load(work / "vendor-security") if "vendorEvidence" in expected else None
    )
    if plan(root, directories, vendor=vendor) != expected:
        raise ValueError("Platform plan differs from retained monitoring and source")
    return expected, proposal(root, expected)


def apply(root: Path, files: dict[str, bytes]) -> None:
    """Write the already-validated proposal."""
    for name, raw in files.items():
        (root / name).write_bytes(raw)


def applies(row: dict[str, Any], profile: str) -> bool:
    """A shared input's targeted finding remains in scope for each inherited image."""
    return (
        row["profile"] == profile
        or (row["type"] == "dotnet" and row["profile"] in BICEP_ROUTES and profile in BICEP_ROUTES)
        or (row["type"] == "apk" and profile in row.get("profiles", []))
    )


def vendor_requests(expected: dict[str, Any], profile: str) -> list[tuple[str, str, str]]:
    """One highest compatible package version must satisfy every targeted finding."""
    selected: dict[tuple[str, str], str] = {}
    for row in expected["findings"]:
        if not applies(row, profile) or row["type"] != "apk" or row["status"] not in ACTIONABLE:
            continue
        key = row["base"], row["component"]
        target = row["target"]
        if key not in selected or version(target, "apk") > version(selected[key], "apk"):
            selected[key] = target
    return [(base, name, selected[base, name]) for base, name in sorted(selected)]


def vendor_checks(expected: dict[str, Any], profile: str) -> list[dict[str, str]]:
    """Require signed repository packages on the recipe's pinned vendor image."""
    reports = []
    for base, name, target in vendor_requests(expected, profile):
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "--pull",
                "always",
                base,
                "sh",
                "-ec",
                (
                    'apk add --no-cache "$1=$2"; apk info -e "$1=$2" >/dev/null; '
                    'printf "verified:%s=%s\\n" "$1" "$2"'
                ),
                "vendor-package",
                name,
                target,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
        if f"verified:{name}={target}" not in result.stdout.splitlines():
            raise ValueError(
                f"Vendor package probe did not install the requested version {name}={target} "
                f"from {base}; stdout={result.stdout[-4000:]!r}; stderr={result.stderr[-4000:]!r}"
            )
        reports.append(
            {
                "base": base,
                "component": name,
                "version": target,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    return reports


def scan(
    root: Path,
    work: Path,
    profile: str,
    variant: str,
    output: Path,
    *,
    validator: Callable[[Path, Path], tuple[dict[str, Any], dict[str, bytes]]] = validate,
) -> None:
    """Build ordinary recipes and retain runtime, vendor and exact-image scan evidence."""
    expected, files = validator(root, work)
    if profile not in expected["affectedProfiles"] or variant not in {"baseline", "candidate"}:
        raise ValueError("Unplanned platform qualification target")
    output.mkdir(parents=True, exist_ok=False)
    if variant == "candidate":
        try:
            write(output / "vendor.json", vendor_checks(expected, profile))
        except subprocess.CalledProcessError as error:
            write(
                output / "vendor-error.json",
                {
                    "outcome": "unavailable",
                    "command": error.cmd,
                    "stdout": error.stdout,
                    "stderr": error.stderr,
                },
            )
            raise
        apply(root, files)
    route = scan_route(profile, "policy")
    environment = {key: value for key, value in os.environ.items() if key != "MAF_PROVIDER_BUILDS"}
    environment["BICEP_ROUTE"] = route
    subprocess.run(
        ["bash", "scripts/build_scan_image.sh", profile],
        cwd=root,
        env=environment,
        check=True,
        timeout=14400,
    )
    details = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", "maf-image-scan:target"], text=True, timeout=60
        )
    )
    source = read(work / "receipt.json")["source"]
    built = record(details, source, profile)
    write(output / "build.json", built)
    image = built["local_image_id"]
    write(output / "runtime.json", check(profile, image, output))
    assess(output, image, require_clean=False)
    write(
        output / "qualification.json",
        {
            "schemaVersion": 1,
            "source": source,
            "profile": profile,
            "variant": variant,
            "route": route,
            "plan": digest(work / "plan.json"),
            "inputs": inputs(root),
        },
    )


def verify_packages(expected: dict[str, Any], sbom: dict[str, Any], profile: str) -> None:
    """A scanner's silence is insufficient when a targeted package disappeared from inventory."""
    for row in expected["findings"]:
        if not applies(row, profile) or row["status"] not in ACTIONABLE:
            continue
        artifacts = [
            item
            for item in sbom["artifacts"]
            if item.get("type") == row["type"] and item.get("name") == row["component"]
        ]
        target = version(row["target"], row["type"])
        versions = [version(item["version"], row["type"]) for item in artifacts]
        if not versions or any(value[:2] != target[:2] or value < target for value in versions):
            raise ValueError("Targeted package fix is absent from the rebuilt inventory")
        if any(
            locations(item) != ["/usr/local/bin/bicep"]
            if row["type"] == "dotnet"
            else not apk_inventory(item)
            for item in artifacts
        ):
            raise ValueError("Targeted package inventory identifies another component location")
        if row["type"] == "apk":
            vendor = "wolfi" if row["base"].startswith("cgr.dev/") else "alpine"
            for item in artifacts:
                prefix = f"pkg:apk/{vendor}/{row['component']}@{item['version']}"
                purl = item.get("purl", "")
                if not (purl == prefix or purl.startswith((prefix + "?", prefix + "#"))):
                    raise ValueError("Targeted package inventory identifies another vendor")


def vendor_resolutions(
    found: list[dict[str, Any]], rows: list[dict[str, Any]], sbom: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Account for stale NVD matches only against exact vendor-fixed APK inventory."""
    unresolved, resolved = [], []
    artifacts = {item["id"]: item for item in sbom["artifacts"]}
    if len(artifacts) != len(sbom["artifacts"]):
        raise ValueError("Ambiguous inventory component IDs")
    for match in found:
        component, vulnerability = match["artifact"], match["vulnerability"]
        actual = artifacts.get(component.get("id"))
        proofs = [
            row
            for row in rows
            if row.get("vendorFix")
            and row["status"] in ACTIONABLE
            and (row["id"], row["component"]) == (vulnerability["id"], component["name"])
        ]
        accepted = None
        for row in proofs:
            proof = row["vendorFix"]
            vendor = vendor_fix_evidence.SOURCES[proof["feed"]][0]
            prefix = f"pkg:apk/{vendor}/{component['name']}@{component['version']}"
            if (
                vulnerability.get("namespace") != "nvd:cpe"
                or vulnerability.get("fix", {}).get("versions")
                or vulnerability.get("fix", {}).get("state")
                not in (None, "", "unknown", "not-fixed")
                or actual is None
                or any(actual.get(key) != component.get(key) for key in ("name", "version", "type"))
                or actual.get("type") != "apk"
                or not apk_inventory(actual)
                or locations(component) != locations(actual)
                or actual.get("metadata", {}).get("originPackage") != proof["sourcePackage"]
                or actual.get("metadata", {}).get("architecture") != "x86_64"
                or not (
                    actual.get("purl") == prefix or actual.get("purl", "").startswith(prefix + "?")
                )
            ):
                continue
            try:
                installed, fixed = (
                    version(actual["version"], "apk"),
                    version(proof["fixedVersion"], "apk"),
                )
            except (TypeError, ValueError):
                continue
            if installed[:2] == fixed[:2] and installed >= fixed:
                accepted = {
                    **proof,
                    "component": row["component"],
                    "artifactId": actual["id"],
                    "installed": actual["version"],
                }
                break
        if accepted is None:
            unresolved.append(match)
        else:
            resolved.append(accepted)
    return unresolved, resolved


def qualify(
    root: Path,
    work: Path,
    results: Path,
    *,
    validator: Callable[[Path, Path], tuple[dict[str, Any], dict[str, bytes]]] = validate,
) -> dict[str, Any]:
    """Require complete baseline/candidate evidence for every inherited affected profile."""
    expected, files = validator(root, work)
    receipt = read(work / "receipt.json")
    baseline_hashes = expected["inputs"]
    candidate_hashes = baseline_hashes | file_hashes(files)
    scans, unresolved = [], []
    baseline_updates = 0
    for variant in ("baseline", "candidate"):
        for profile in expected["affectedProfiles"]:
            directory = results / f"{variant}-{profile}"
            identity = read(directory / "qualification.json")
            route = scan_route(profile, "policy")
            if identity != {
                "schemaVersion": 1,
                "source": receipt["source"],
                "profile": profile,
                "variant": variant,
                "route": route,
                "plan": digest(work / "plan.json"),
                "inputs": baseline_hashes if variant == "baseline" else candidate_hashes,
            }:
                raise ValueError("Platform result identifies another source, route or proposal")
            built = read(directory / "build.json")
            image = built["local_image_id"]
            if built["source_commit"] != receipt["source"] or built["profile"] != profile:
                raise ValueError("Platform build identity differs")
            runtime = read(directory / "runtime.json")
            cleanup = (
                "sandboxesRemaining" if profile == "bicep-docker-sbx" else "containersRemaining"
            )
            if (
                runtime.get("passed") is not True
                or runtime.get("profile") != profile
                or runtime.get("imageId") != image
                or runtime.get(cleanup) != []
            ):
                raise ValueError("Platform runtime qualification is incomplete")
            sbom, report, assessment = (
                read(directory / name)
                for name in ("sbom.syft.json", "grype.json", "assessment.json")
            )
            verify_inventory(sbom, image)
            if scan_result(report, image, assessment["assessedAt"], sbom=sbom) != assessment:
                raise ValueError("Platform assessment differs from its scan bytes")
            if profile in BICEP_ROUTES:
                if evidence_route(profile, image, runtime, sbom) != route:
                    raise ValueError("Bicep evidence qualifies another compiler route")
                build = runtime["bicepAvm" if profile == "bicep-avm" else "bicep"]["build"]
                policy_data = policy(root / BICEP / "source.json")
                if variant == "candidate":
                    for update in expected["updates"]:
                        if update["type"] == "dotnet":
                            policy_data["packages"][update["component"]] = update["after"]
                if any(build.get(key) != value for key, value in policy_data.items()):
                    raise ValueError("Bicep receipt differs from the planned compiler policy")
                verify_bicep_inventory(sbom, runtime)
            if profile in PROVIDER_MANIFESTS:
                verify_engine(sbom, profile, read(root / RECIPE))
                verify_providers(sbom, read(root / CONTEXT / PROVIDER_MANIFESTS[profile]))
            rows = [row for row in expected["findings"] if applies(row, profile)]
            wanted = {(row["id"], row["component"]) for row in rows if row["status"] in ACTIONABLE}
            found = remaining(report, wanted)
            resolved = []
            if variant == "baseline":
                updates = {
                    (row["id"], row["component"]) for row in rows if row["status"] == "update"
                }
                baseline_updates += len(remaining(report, updates))
            else:
                vendor = read(directory / "vendor.json")
                required = set(vendor_requests(expected, profile))
                observed = {(row["base"], row["component"], row["version"]) for row in vendor}
                if observed != required or len(vendor) != len(required):
                    raise ValueError("Vendor availability evidence is incomplete")
                for row in vendor:
                    if (
                        f"verified:{row['component']}={row['version']}"
                        not in row["stdout"].splitlines()
                    ):
                        raise ValueError("Vendor evidence did not install the requested package")
                outstanding, resolved = vendor_resolutions(found, rows, sbom)
                if outstanding:
                    unresolved.extend(outstanding)
                else:
                    verify_packages(expected, sbom, profile)
            scans.append(
                {
                    "profile": profile,
                    "variant": variant,
                    "imageId": image,
                    "assessment": assessment,
                    "targetedFindings": len(found),
                    **(
                        {"vendorResolvedFindings": resolved} if "vendorEvidence" in expected else {}
                    ),
                }
            )
    status = (
        "unresolved" if unresolved else "ready" if files and baseline_updates else "release-needed"
    )
    if not expected["affectedProfiles"]:
        status = "no-supported-fix"
    return {
        "schemaVersion": 1,
        "kind": "platform",
        "status": status,
        "source": receipt["source"],
        "plan": digest(work / "plan.json"),
        "files": file_hashes(files),
        "scans": scans,
        "unresolved": unresolved,
    }


def main() -> None:
    """Expose acquisition, bounded edits, qualification and guarded PR delivery."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("acquire", "apply", "scan", "qualify", "publish"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--variant", choices=("baseline", "candidate"))
    parser.add_argument("--bot")
    parser.add_argument("--artifact-id", type=int)
    args = parser.parse_args()
    run_id = None
    if args.command == "publish":
        try:
            run_id = positive_id(int(os.environ.get("GITHUB_RUN_ID", "")))
        except ValueError:
            parser.error("publish requires a positive GITHUB_RUN_ID from GitHub Actions")
    work = args.work.resolve()
    if args.command == "acquire":
        result = acquire(
            GitHub(),
            ROOT,
            work,
            selected_profiles=RUNTIME_RECIPES,
            planner=acquire_plan,
            require_all_profiles=False,
        )
        if result is None:
            acquisition_summary(work, None)
            outputs({"status": "deferred", "profiles": "[]"})
            return
        conflicts = competing_pulls(GitHub(), {item["path"] for item in result["updates"]})
        write(work / "competing-pulls.json", conflicts)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            counts = Counter(row["status"] for row in result["findings"])
            with Path(summary).open("a", encoding="utf-8") as stream:
                stream.write("### Platform remediation plan\n\n")
                for status, count in sorted(counts.items()):
                    stream.write(f"- `{status}`: {count}\n")
                stream.write(f"\nProposed package edits: {len(result['updates'])}. ")
                stream.write(f"Competing source PRs: {conflicts}. ")
                stream.write(
                    "Build and scan qualification is still required; no published image recovery is claimed.\n"
                )
        outputs(
            {
                "source": read(work / "receipt.json")["source"],
                "profiles": json.dumps(result["affectedProfiles"] if not conflicts else []),
            }
        )
    elif args.command == "apply":
        apply(ROOT, validate(ROOT, work)[1])
    elif args.command == "scan":
        if args.profile is None or args.variant is None or args.results is None:
            parser.error("scan requires profile, variant and results")
        scan(ROOT, work, args.profile, args.variant, args.results)
    else:
        if args.results is None:
            parser.error("qualification requires results")
        report = qualify(ROOT, work, args.results)
        if args.command == "qualify":
            write(work / "qualification.json", report)
        try:
            current(GitHub(), read(work / "receipt.json"))
        except (EvidenceChanging, HistoryAdvanced) as error:
            result = deferred(work, error, args.command)
            if args.command == "qualify":
                write(work / "qualified.json", report | result)
                outputs({"ready": "false", "status": "deferred"})
            else:
                write(work / "publication.json", result)
            return
        conflicts = competing_pulls(GitHub(), set(report["files"]))
        if conflicts:
            raise ValueError(f"Existing source PRs own the proposed inputs: {conflicts}")
        if args.command == "qualify":
            write(work / "qualified.json", report)
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with Path(summary).open("a", encoding="utf-8") as stream:
                    stream.write(f"\nPlatform qualification: **{report['status']}**. ")
                    stream.write(
                        "Unrelated findings remain in the retained assessments; image publication remains maintainer-controlled.\n"
                    )
            outputs({"ready": "true" if report["status"] == "ready" else "false"})
        else:
            if args.bot is None or args.artifact_id is None:
                parser.error("publish requires bot and artifact-id")
            if report != read(work / "qualified.json"):
                raise ValueError("Platform qualification changed before publication")
            try:
                result = publish(
                    PlatformGitHub(set(report["files"])),
                    read(work / "receipt.json"),
                    report,
                    validate(ROOT, work)[1],
                    args.bot,
                    f"https://github.com/{REPOSITORY}/actions/runs/"
                    f"{run_id}/artifacts/"
                    f"{positive_id(args.artifact_id)}",
                )
            except (EvidenceChanging, HistoryAdvanced) as error:
                result = deferred(work, error, "publish")
            write(work / "publication.json", result)
            print(json.dumps(result))


if __name__ == "__main__":
    main()
