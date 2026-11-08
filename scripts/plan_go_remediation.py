"""Plan Go source-overlay updates from internally consistent retained image evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from container_release import (
    IDENTITY_FIELDS,
    OPTIONAL_IDENTITY_FIELDS,
    ROOT,
    decode,
    scan_result,
    validate_identity,
)
from image_security_evidence import PROVIDER_MANIFESTS, verify_inventory
from terraform_dependencies import load_manifest

CONTEXT = "images/terraform-sandbox"
RECIPE = f"{CONTEXT}/image.json"
TOOLING = (
    "scripts/plan_go_remediation.py",
    "scripts/prepare_go_remediation.py",
    "scripts/go_remediation_worker.py",
    "scripts/container_release.py",
    "scripts/image_security_evidence.py",
    "scripts/chrome_inventory.py",
    "scripts/sqlite_vulnerability.py",
    "scripts/terraform_dependencies.py",
    "scripts/terraform_manifest.py",
    f"{CONTEXT}/build_providers.py",
    f"{CONTEXT}/runner.py",
)


def digest(path: Path) -> str:
    """Bind plans to original bytes, including line endings."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def semver(value: str) -> tuple[int, int, int]:
    """Limit automatic proposals to stable Go module releases."""
    if not isinstance(value, str) or not re.fullmatch(
        r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value
    ):
        raise ValueError("Unsupported Go module version")
    major, minor, patch = value.removeprefix("v").split(".")
    return int(major), int(minor), int(patch)


def module_name(value: str) -> bool:
    """Accept module paths without command flags, traversal or version expressions."""
    return bool(
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
        and "." in value.split("/")[0]
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def fixed_version(installed: str, fix: dict[str, Any]) -> str | None:
    """Choose the highest listed stable fix on the installed major line."""
    current = semver(installed)
    if fix.get("state") != "fixed":
        return None
    candidates = []
    for value in fix.get("versions") or []:
        try:
            parsed = semver(value)
        except ValueError:
            continue
        if parsed[0] == current[0] and parsed > current:
            candidates.append(parsed)
    return "v" + ".".join(map(str, max(candidates))) if candidates else None


def inputs(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Discover all provider locks so shared declarations cannot silently diverge."""
    paths = [root / RECIPE, *sorted((root / CONTEXT).glob("dependencies.*.json"))]
    documents = {}
    hashes = {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Source input escapes the checkout")
        raw = path.read_bytes()
        document = decode(raw.decode("utf-8-sig"))
        name = path.relative_to(root).as_posix()
        if not name.endswith(".policy.json"):
            documents[name] = document if name == RECIPE else load_manifest(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    for name in ("Dockerfile", "prepared.Dockerfile"):
        path = root / CONTEXT / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Source input escapes the checkout")
        hashes[f"{CONTEXT}/{name}"] = digest(path)
    return documents, hashes


def locations(artifact: dict[str, Any]) -> list[str]:
    """Preserve every binary occurrence rather than assigning a module by its name alone."""
    result = sorted({entry["path"] for entry in artifact.get("locations", [])})
    if any(not isinstance(path, str) for path in result):
        raise ValueError("Invalid component locations")
    return result


def evidence(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """Verify local consistency; release provenance must be established by the caller."""
    files = ("observation.json", "sbom.syft.json", "grype.json")
    for name in files:
        if (directory / name).is_symlink():
            raise ValueError("Evidence symlinks are not supported")
    snapshots = {name: (directory / name).read_bytes() for name in files}
    observation = decode(snapshots["observation.json"].decode("utf-8-sig"))
    candidate, assessment = observation["candidate"], observation["assessment"]
    validate_identity(candidate)
    if candidate["assessedManifestDigest"] != candidate["registryDigest"]:
        raise ValueError("Remediation requires a single assessed registry manifest")
    if assessment.get("digest") != candidate["registryDigest"]:
        raise ValueError("Assessment identifies another registry digest")
    sbom, report = (
        decode(snapshots[name].decode("utf-8-sig")) for name in ("sbom.syft.json", "grype.json")
    )
    verify_inventory(sbom, candidate["imageId"])
    verified = scan_result(report, candidate["imageId"], assessment["assessedAt"], sbom=sbom)
    if any(assessment.get(key) != value for key, value in verified.items()):
        raise ValueError("Observation differs from retained scan evidence")
    artifacts = {item["id"]: item for item in sbom["artifacts"]}
    if len(artifacts) != len(sbom["artifacts"]):
        raise ValueError("Ambiguous inventory component IDs")
    excluded = {(item["id"], item["artifactId"]) for item in verified.get("notAffected", [])}
    matches = []
    for match in report["matches"]:
        artifact, vulnerability = match["artifact"], match["vulnerability"]
        if vulnerability["severity"] not in {"High", "Critical"}:
            continue
        actual = artifacts.get(artifact["id"])
        if (
            actual is None
            or any(actual.get(key) != artifact.get(key) for key in ("name", "version", "type"))
            or locations(actual) != locations(artifact)
        ):
            raise ValueError("Vulnerable component differs from retained inventory")
        if (vulnerability["id"], artifact["id"]) not in excluded:
            matches.append({**match, "artifact": actual})
    identity = {key: candidate[key] for key in IDENTITY_FIELDS}
    identity.update({key: candidate[key] for key in OPTIONAL_IDENTITY_FIELDS if key in candidate})
    identity["assessedAt"] = assessment["assessedAt"]
    return (
        identity,
        matches,
        {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshots.items()},
    )


def owners(
    documents: dict[str, Any], profile: str, guest_path: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """Map only known engine paths or exact provider identities in the selected profile."""
    if profile not in PROVIDER_MANIFESTS:
        return []
    engine = profile.split("-", 1)[0]
    executable = "tofu" if engine == "opentofu" else "terraform"
    if guest_path == f"/usr/local/bin/{executable}":
        return [(RECIPE, engine, documents[RECIPE]["engines"][engine]["source_build"])]
    manifest = documents[f"{CONTEXT}/{PROVIDER_MANIFESTS[profile]}"]
    matching = []
    for provider in manifest["providers"]:
        name = provider["source"].rsplit("/", 1)[-1]
        prefix = (
            f"/opt/maf-terraform/mirror/{provider['source']}/{provider['version']}/"
            f"{provider['platform']}/terraform-provider-{name}"
        )
        if re.fullmatch(
            re.escape(prefix) + rf"(?:_v{re.escape(provider['version'])}(?:_x\d+)?)?", guest_path
        ):
            matching.append(provider)
    if len(matching) != 1 or "source_build" not in matching[0]:
        return []
    selected = matching[0]
    identity = tuple(selected[key] for key in ("source", "version", "platform"))
    result = []
    for path, document in documents.items():
        for index, provider in enumerate(document.get("providers", [])):
            if tuple(provider[key] for key in ("source", "version", "platform")) == identity:
                source = provider.get("source_build")
                if source is None:
                    continue
                if any(
                    source[key] != selected["source_build"][key]
                    for key in ("repository", "revision", "artifact", "go_version")
                ):
                    raise ValueError(
                        "Shared provider declarations have different source identities"
                    )
                result.append((path, str(index), source))
    return result


def plan(root: Path, directories: list[Path]) -> dict[str, Any]:
    """Produce deterministic proposals without writing source files or resolving dependencies."""
    documents, hashes = inputs(root)
    records, findings = [], []
    updates: dict[tuple[str, str, str], dict[str, Any]] = {}
    profiles = set()
    for directory in directories:
        identity, matches, evidence_hashes = evidence(directory)
        records.append({**identity, "files": evidence_hashes})
        profiles.add(identity["profile"])
        for match in matches:
            artifact, vulnerability = match["artifact"], match["vulnerability"]
            row = {
                "profile": identity["profile"],
                "imageId": identity["imageId"],
                "id": vulnerability["id"],
                "severity": vulnerability["severity"],
                "module": artifact["name"],
                "installed": artifact["version"],
                "guestPaths": locations(artifact),
                "fix": vulnerability.get("fix") or {},
                "decisions": [],
            }
            findings.append(row)
            decisions = row["decisions"]
            if artifact["type"] != "go-module" or not module_name(artifact["name"]):
                decisions.append({"status": "unsupported-component"})
                continue
            if artifact["name"] == artifact.get("metadata", {}).get("mainModule"):
                decisions.append({"status": "source-version-upgrade"})
                continue
            try:
                target = fixed_version(artifact["version"], row["fix"])
            except ValueError:
                decisions.append({"status": "unsupported-version"})
                continue
            if target is None:
                decisions.append({"status": "no-compatible-fix"})
                continue
            if not row["guestPaths"]:
                decisions.append({"status": "unmapped-binary"})
            for guest_path in row["guestPaths"]:
                mapped = owners(documents, identity["profile"], guest_path)
                if not mapped:
                    decisions.append({"guestPath": guest_path, "status": "unmapped-binary"})
                for path, owner, source in mapped:
                    previous = source["modules"].get(artifact["name"])
                    decision = {"path": path, "owner": owner, "target": target}
                    try:
                        if previous is not None and semver(previous)[0] != semver(target)[0]:
                            raise ValueError("Different major")
                        if previous is not None and semver(previous) >= semver(target):
                            decisions.append({**decision, "status": "override-already-sufficient"})
                            continue
                    except ValueError:
                        decisions.append({**decision, "status": "unsupported-override"})
                        continue
                    decisions.append({**decision, "status": "update"})
                    key = path, owner, artifact["name"]
                    update = updates.setdefault(
                        key,
                        {
                            "path": path,
                            "owner": owner,
                            "module": artifact["name"],
                            "before": previous,
                            "after": target,
                        },
                    )
                    if semver(target)[0] != semver(update["after"])[0]:
                        raise ValueError(f"Conflicting target majors for {key}")
                    if semver(target) > semver(update["after"]):
                        update["after"] = target

    def stable(row: dict[str, Any]) -> str:
        return json.dumps(row, sort_keys=True)

    return {
        "schemaVersion": 1,
        "inputs": hashes,
        "tooling": {name: digest(ROOT / name) for name in TOOLING},
        "evidence": sorted({stable(row): row for row in records}.values(), key=stable),
        "findings": sorted({stable(row): row for row in findings}.values(), key=stable),
        "updates": [updates[key] for key in sorted(updates)],
        "profiles": sorted(profiles),
        "affectedProfiles": sorted(PROVIDER_MANIFESTS) if updates else [],
    }


def main() -> None:
    """Write an offline plan without resolving modules or changing build inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--evidence", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.is_relative_to(args.root.resolve()):
        raise ValueError("Plan output must be outside the source checkout")
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Plan output must not already exist")
    result = plan(args.root, args.evidence)
    payload = (json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(payload)


if __name__ == "__main__":
    main()
