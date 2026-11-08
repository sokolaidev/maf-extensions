"""Qualify coordinated Go-image proposals without publishing images or changing release state."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from check_container_release_image import check
from container_release import ROOT, read, scan_result, write
from container_release_history import GitHub
from image_release_needed import git
from image_remediation_evidence import acquire_with_retry as acquire
from image_remediation_evidence import current
from image_security_evidence import PROVIDER_MANIFESTS, record, verify_inventory, verify_providers
from plan_go_remediation import CONTEXT, RECIPE, digest, inputs, locations, owners, semver
from plan_go_remediation import plan as make_plan
from prepare_container_release import assess
from prepare_go_remediation import build_shard, prepare, provider_requests
from terraform_dependencies import load_manifest, verify_built_provider


def proposal_files(root: Path, directory: Path) -> dict[str, bytes]:
    """Accept only planned module overlays and their regenerated provider lock fields."""
    expected = read(directory / "plan.json")
    proposal = read(directory / "proposal.json")
    documents, hashes = inputs(root)
    if hashes != expected["inputs"] or proposal.get("schemaVersion") != 1:
        raise ValueError("Proposal inputs changed")
    changed = {item["path"] for item in expected["updates"]}
    if not changed or changed != proposal["files"].keys() or not changed <= documents.keys():
        raise ValueError("Proposal file set differs from planned edits")
    wanted = copy.deepcopy(documents)
    planned_providers = {
        (item["path"], item["owner"]) for item in expected["updates"] if item["path"] != RECIPE
    }
    for update in proposal["companionUpdates"]:
        if (update["path"], update["owner"]) not in planned_providers:
            raise ValueError("Unplanned companion owner")
        source = documents[update["path"]]["providers"][int(update["owner"])]["source_build"]
        requested = {
            item["module"]
            for item in expected["updates"]
            if (item["path"], item["owner"]) == (update["path"], update["owner"])
        }
        if update["module"] not in source["modules"] and update["module"] not in requested:
            raise ValueError("Unplanned companion module")
    provider_keys = set()
    for update in [*expected["updates"], *proposal["companionUpdates"]]:
        name, owner, module = update["path"], update["owner"], update["module"]
        if name not in changed:
            raise ValueError("Unplanned companion file")
        source = (
            wanted[name]["engines"][owner]["source_build"]
            if name == RECIPE
            else wanted[name]["providers"][int(owner)]["source_build"]
        )
        if source["modules"].get(module) != update["before"]:
            raise ValueError("Module update does not start at the expected version")
        after = semver(update["after"])
        if update["before"] is not None:
            before = semver(update["before"])
            if before[0] != after[0] or after <= before:
                raise ValueError("Unsafe companion module update")
        source["modules"][module] = update["after"]
        if name != RECIPE:
            provider_keys.add((name, int(owner)))
    actual_keys = [(item["path"], item["index"]) for item in proposal["providers"]]
    if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != provider_keys:
        raise ValueError("Provider package set differs from planned edits")
    payload = {}
    parsed = {}
    for name in sorted(changed):
        path = directory / "files" / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Proposal path escapes its artifact")
        if digest(path) != proposal["files"][name]:
            raise ValueError("Proposal bytes changed")
        payload[name] = path.read_bytes()
        parsed[name] = read(path) if name == RECIPE else load_manifest(payload[name])
    for item in proposal["providers"]:
        name, index = item["path"], item["index"]
        provider = parsed[name]["providers"][index]
        source = provider["source_build"]
        if item["package"] != f"providers/{source['sha256']}.zip":
            raise ValueError("Unexpected provider package path")
        package = directory / item["package"]
        if package.is_symlink() or not package.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Provider package escapes its artifact")
        verify_built_provider(provider, package.read_bytes())
        target = wanted[name]["providers"][index]["source_build"]
        for field in ("sha256", "module_files"):
            target[field] = source[field]
    if any(parsed[name] != wanted[name] for name in changed):
        raise ValueError("Proposal changes fields outside the module overlay and lock")
    if all(payload[name] == (root / name).read_bytes() for name in changed):
        raise ValueError("Empty remediation proposal")
    return payload


def apply(root: Path, directory: Path) -> None:
    """Validate the entire artifact before writing any source file."""
    for name, raw in proposal_files(root, directory).items():
        (root / name).write_bytes(raw)


def validate_plan(root: Path, work: Path) -> None:
    """Recompute artifact instructions from the acquired monitor bytes and current source."""
    receipt = read(work / "receipt.json")
    expected = read(work / "plan.json")
    if (
        make_plan(root, [work / "evidence" / name for name in receipt["directories"]]) != expected
        or read(work / "proposal/plan.json") != expected
    ):
        raise ValueError("Proposal plan differs from acquired evidence")


def targets(plan: dict[str, Any]) -> set[tuple[str, str]]:
    """Keep vulnerability identities even when a later database changes their severity."""
    return {
        (item["id"], item["module"])
        for item in plan["findings"]
        if any(decision["status"] == "update" for decision in item["decisions"])
    }


def remaining(report: dict[str, Any], wanted: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Include suppressed matches; scanner configuration cannot hide a targeted finding."""
    return [
        item
        for item in [*report["matches"], *report.get("ignoredMatches", [])]
        if (item["vulnerability"]["id"], item["artifact"]["name"]) in wanted
    ]


def verify_modules(
    documents: dict[str, Any], sbom: dict[str, Any], profile: str, plan: dict[str, Any]
) -> None:
    """Require the proposed modules in each affected executable's inventory."""
    manifest_name = f"{CONTEXT}/{PROVIDER_MANIFESTS[profile]}"
    engine = profile.split("-", 1)[0]
    for update in plan["updates"]:
        name, owner = update["path"], update["owner"]
        if name != manifest_name and not (name == RECIPE and owner == engine):
            continue
        source = (
            documents[name]["engines"][owner]["source_build"]
            if name == RECIPE
            else documents[name]["providers"][int(owner)]["source_build"]
        )
        expected = semver(source["modules"][update["module"]])
        versions = []
        for artifact in sbom["artifacts"]:
            if artifact.get("type") != "go-module" or artifact["name"] != update["module"]:
                continue
            for guest_path in locations(artifact):
                if any(
                    (path, identity) == (name, owner)
                    for path, identity, _ in owners(documents, profile, guest_path)
                ):
                    versions.append(semver(artifact["version"]))
        if not versions or any(
            version[0] != expected[0] or version < expected for version in versions
        ):
            raise ValueError("Built executable does not contain its proposed module fix")


def verify_engine(sbom: dict[str, Any], profile: str, recipe: dict[str, Any]) -> None:
    """Require the engine main module and its Go runtime at the declared executable path."""
    engine = profile.split("-", 1)[0]
    expected = recipe["engines"][engine]
    guest_path = f"/usr/local/bin/{expected['executable']}"
    module = {
        "terraform": "github.com/hashicorp/terraform",
        "opentofu": "github.com/opentofu/opentofu",
    }[engine]
    artifacts = [
        item
        for item in sbom["artifacts"]
        if item.get("type") == "go-module" and guest_path in locations(item)
    ]
    mains = [
        item
        for item in artifacts
        if item.get("name") == module and item.get("metadata", {}).get("mainModule") == module
    ]
    if len(mains) != 1:
        raise ValueError(f"Engine main module is missing or ambiguous: {guest_path}")
    compiler = mains[0]["metadata"].get("goCompiledVersion", "")
    version = mains[0].get("version")
    versions = {item.get("version") for item in artifacts if item.get("name") == "stdlib"}
    if (
        not isinstance(version, str)
        or version.split("+", 1)[0] != f"v{expected['version']}"
        or not isinstance(compiler, str)
        or not re.fullmatch(r"go\d+\.\d+\.\d+", compiler)
        or versions != {compiler}
    ):
        raise ValueError(f"Engine Go inventory is incomplete or contradictory: {guest_path}")


def scan(root: Path, work: Path, profile: str, variant: str, output: Path) -> None:
    """Build ordinary recipes on a fresh runner, then test and scan the exact local image ID."""
    if profile not in PROVIDER_MANIFESTS or variant not in {"baseline", "candidate"}:
        raise ValueError("Unsupported qualification target")
    receipt = read(work / "receipt.json")
    if git(root, "rev-parse", "HEAD").strip() != receipt["source"]:
        raise ValueError("Qualification checkout differs from the acquired main commit")
    if variant == "candidate":
        validate_plan(root, work)
        apply(root, work / "proposal")
    elif inputs(root)[1] != read(work / "plan.json")["inputs"]:
        raise ValueError("Baseline inputs changed")
    # A supplied provider context would bypass the ordinary builder's lock replay.
    environment = {key: value for key, value in os.environ.items() if key != "MAF_PROVIDER_BUILDS"}
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
    build = record(details, receipt["source"], profile)
    output.mkdir(parents=True, exist_ok=False)
    write(output / "build.json", build)
    image = build["local_image_id"]
    write(output / "runtime.json", check(profile, image, output))
    assess(output, image, require_clean=False)
    verify_engine(read(output / "sbom.syft.json"), profile, read(root / RECIPE))
    write(
        output / "qualification.json",
        {
            "schemaVersion": 1,
            "source": receipt["source"],
            "profile": profile,
            "variant": variant,
            "plan": digest(work / "plan.json"),
            "inputs": inputs(root)[1],
        },
    )


def qualify(root: Path, work: Path, results: Path) -> dict[str, Any]:
    """Recompute every scan and require all affected profiles before admitting a source PR."""
    plan = read(work / "plan.json")
    receipt = read(work / "receipt.json")
    if git(root, "rev-parse", "HEAD").strip() != receipt["source"]:
        raise ValueError("Qualification base changed")
    validate_plan(root, work)
    payload = proposal_files(root, work / "proposal")
    original, baseline_hashes = inputs(root)
    proposed = {**original, **{name: json.loads(raw) for name, raw in payload.items()}}
    candidate_hashes = {**baseline_hashes, **read(work / "proposal/proposal.json")["files"]}
    wanted = targets(plan)
    if not wanted or plan["affectedProfiles"] != sorted(PROVIDER_MANIFESTS):
        raise ValueError("Incomplete qualification profile set")
    baseline_findings = []
    scans = []
    for variant in ("baseline", "candidate"):
        documents = original if variant == "baseline" else proposed
        for profile in sorted(PROVIDER_MANIFESTS):
            directory = results / f"{variant}-{profile}"
            identity = read(directory / "qualification.json")
            if identity != {
                "schemaVersion": 1,
                "source": receipt["source"],
                "profile": profile,
                "variant": variant,
                "plan": digest(work / "plan.json"),
                "inputs": baseline_hashes if variant == "baseline" else candidate_hashes,
            }:
                raise ValueError("Qualification identifies another proposal")
            build = read(directory / "build.json")
            image = build["local_image_id"]
            if build["profile"] != profile or build["source_commit"] != receipt["source"]:
                raise ValueError("Build identifies another source or profile")
            runtime = read(directory / "runtime.json")
            if (
                runtime.get("passed") is not True
                or runtime.get("profile") != profile
                or runtime.get("imageId") != image
                or runtime.get("containersRemaining") != []
            ):
                raise ValueError("Runtime qualification failed or identifies another image")
            sbom, report, assessment = (
                read(directory / name)
                for name in ("sbom.syft.json", "grype.json", "assessment.json")
            )
            verify_inventory(sbom, image)
            verify_engine(sbom, profile, documents[RECIPE])
            verify_providers(sbom, documents[f"{CONTEXT}/{PROVIDER_MANIFESTS[profile]}"])
            if scan_result(report, image, assessment["assessedAt"], sbom=sbom) != assessment:
                raise ValueError("Assessment differs from retained scan bytes")
            found = remaining(report, wanted)
            if variant == "candidate":
                if found:
                    raise ValueError("Targeted findings remain in a rebuilt candidate")
                verify_modules(documents, sbom, profile, plan)
            else:
                baseline_findings.extend(found)
            scans.append(
                {
                    "variant": variant,
                    "profile": profile,
                    "imageId": image,
                    "assessment": assessment,
                    "targetedFindings": len(found),
                }
            )
    return {
        "schemaVersion": 1,
        "status": "ready" if baseline_findings else "source-already-fixed",
        "source": receipt["source"],
        "plan": digest(work / "plan.json"),
        "files": read(work / "proposal/proposal.json")["files"],
        "scans": scans,
    }


def go_version(root: Path, plan: dict[str, Any]) -> str:
    """Select the declared provider toolchain; mixed toolchains need an explicit adapter."""
    documents, _ = inputs(root)
    versions = {
        documents[item["path"]]["providers"][int(item["owner"])]["source_build"]["go_version"]
        for item in plan["updates"]
        if item["path"] != RECIPE
    }
    if not versions:
        return ""
    if len(versions) != 1 or not re.fullmatch(r"go[0-9]+\.[0-9]+\.[0-9]+", next(iter(versions))):
        raise ValueError("Provider proposal requires incompatible Go toolchains")
    return next(iter(versions)).removeprefix("go")


def acquisition_summary(work: Path, result: dict[str, Any] | None) -> str:
    """Explain skipped builds without implying that published images are repaired."""
    if result is None:
        status = "deferred"
        message = (
            "Acquisition deferred while monitoring or the release catalogue is changing. "
            "The next monitor completion or scheduled run retries automatically."
        )
    elif result["updates"]:
        status = "updates-planned"
        message = f"Planned {len(result['updates'])} source updates; qualification is pending."
    else:
        sufficient = sum(
            decision["status"] == "override-already-sufficient"
            for finding in result["findings"]
            for decision in finding["decisions"]
        )
        status = "source-already-fixed" if sufficient else "no-supported-updates"
        message = (
            f"Source overrides already cover {sufficient} finding decisions; "
            "replacement image publication and fresh monitoring remain pending."
            if sufficient
            else "No supported source updates were found; this does not establish image health."
        )
        message += " See plan.json for remaining findings and unsupported components."
    write(work / "acquisition.json", {"status": status, "message": message})
    print(message, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(f"## Image remediation: {status}\n\n{message}\n")
    return status


def main() -> None:
    """Expose the read-only acquisition, preparation, image checks and aggregate gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("acquire", "provider", "prepare", "apply", "scan", "qualify")
    )
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--profile", choices=sorted(PROVIDER_MANIFESTS))
    parser.add_argument("--variant", choices=("baseline", "candidate"))
    parser.add_argument("--results", type=Path)
    parser.add_argument("--shard")
    args = parser.parse_args()
    work = args.work.resolve()
    if args.command == "acquire":
        result = acquire(GitHub(), ROOT, work)
        status = acquisition_summary(work, result)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"status={status}\n")
            if result is None:
                output.write("updates=false\nproviders=[]\n")
                return
            output.write(f"source={read(work / 'receipt.json')['source']}\n")
            output.write(f"updates={'true' if result['updates'] else 'false'}\n")
            output.write(f"go={go_version(ROOT, result)}\n")
            output.write(f"providers={json.dumps(sorted(provider_requests(ROOT, result)))}\n")
    elif args.command == "provider":
        if args.shard is None or args.results is None:
            parser.error("provider requires shard and results")
        receipt = read(work / "receipt.json")
        build_shard(
            ROOT,
            [work / "evidence" / name for name in receipt["directories"]],
            read(work / "plan.json"),
            args.shard,
            args.results,
        )
    elif args.command == "prepare":
        receipt = read(work / "receipt.json")
        if args.results is not None:
            args.results.mkdir(parents=True, exist_ok=True)
        prepare(
            ROOT,
            [work / "evidence" / name for name in receipt["directories"]],
            read(work / "plan.json"),
            work / "proposal",
            shards=args.results,
        )
        current(GitHub(), receipt)
    elif args.command == "apply":
        validate_plan(ROOT, work)
        apply(ROOT, work / "proposal")
    elif args.command == "scan":
        if args.profile is None or args.variant is None or args.results is None:
            parser.error("scan requires profile, variant and results")
        scan(ROOT, work, args.profile, args.variant, args.results)
    else:
        if args.results is None:
            parser.error("qualify requires results")
        result = qualify(ROOT, work, args.results)
        current(GitHub(), read(work / "receipt.json"))
        write(work / "qualified.json", result)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"ready={'true' if result['status'] == 'ready' else 'false'}\n")


if __name__ == "__main__":
    main()
