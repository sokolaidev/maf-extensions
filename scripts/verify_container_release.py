"""Verify a release's three attestations and retained evidence before executing image code."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from build_hyperlight_aks_image import SOURCE_URL, verify_image
from container_release import (
    COMPLETION,
    ISSUER,
    PREFIX,
    PROVENANCE,
    REPOSITORY,
    SIGNER,
    SPDX,
    digest,
    read,
    require_digest,
    validate_identity,
)
from container_release_oci import MANIFEST
from container_release_status import report


def policy_flags(expected: dict[str, Any], predicate: str) -> list[str]:
    """Pin the issuer, hosted runner, source and signer independently of candidate evidence."""
    validate_identity(expected)
    return [
        "--hostname",
        "github.com",
        "--repo",
        REPOSITORY,
        "--cert-identity",
        SIGNER,
        "--cert-oidc-issuer",
        ISSUER,
        "--source-digest",
        expected["sourceCommit"],
        "--signer-digest",
        expected["sourceCommit"],
        "--source-ref",
        "refs/heads/main",
        "--deny-self-hosted-runners",
        "--predicate-type",
        predicate,
        "--format",
        "json",
    ]


def statement(result: Any, expected: dict[str, Any], predicate: str) -> dict[str, Any]:
    """Select a verified statement only after matching its complete subject and claim type."""
    if not isinstance(result, list) or not result:
        raise ValueError("No authenticated attestation was returned")
    accepted = []
    for item in result:
        value = item.get("verificationResult", {}).get("statement", {})
        if (
            value.get("_type") != "https://in-toto.io/Statement/v1"
            or value.get("predicateType") != predicate
        ):
            raise ValueError("Unexpected verified statement type")
        subject = value.get("subject")
        if subject != [
            {
                "name": f"{PREFIX}/{expected['profile']}",
                "digest": {"sha256": expected["registryDigest"].removeprefix("sha256:")},
            }
        ]:
            raise ValueError("Attestation subject does not match the selected image")
        claim = value.get("predicate")
        if not isinstance(claim, dict):
            raise ValueError("Attestation has no predicate object")
        accepted.append(claim)
    if any(value != accepted[0] for value in accepted[1:]):
        raise ValueError("Conflicting authenticated predicates")
    return accepted[0]


def validate_completion(claim: dict[str, Any], expected: dict[str, Any]) -> str:
    """Refuse a signed but incomplete, misidentified or malformed completion statement."""
    if (
        type(claim.get("schemaVersion")) is not int
        or claim["schemaVersion"] != 1
        or claim.get("state") != "completed"
    ):
        raise ValueError("Release has no supported completed state")
    for name in (
        "profile",
        "version",
        "sourceCommit",
        "sourceRef",
        "registryDigest",
        "assessedManifestDigest",
    ):
        if not isinstance(claim.get(name), str) or claim[name] != expected[name]:
            raise ValueError(f"Completion {name} does not match the selected release")
    evidence_hash = require_digest(claim.get("evidenceIndexSha256"))
    if expected.get("evidenceIndexSha256") and expected["evidenceIndexSha256"] != evidence_hash:
        raise ValueError("Completion evidence differs from the approved index")
    return evidence_hash


def verify_evidence(directory: Path, expected_hash: str) -> None:
    """Verify the signed index and every referenced evidence file without following links."""
    index = directory / "evidence-index.json"
    if directory.is_symlink() or index.is_symlink() or digest(index) != expected_hash:
        raise ValueError("Release evidence index hash mismatch")
    records = read(index)
    if not isinstance(records, dict) or not records or len(records) > 1024:
        raise ValueError("Invalid evidence index")
    required = {
        "manifest.json",
        "build.json",
        "sbom.syft.json",
        "sbom.spdx.json",
        "grype.json",
        "runtime.json",
        "provenance.jsonl",
        "sbom.jsonl",
    }
    if not required <= records.keys():
        raise ValueError("Release evidence is incomplete")
    for name, expected in records.items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) or name in {
            "evidence-index.json",
            "completion.jsonl",
        }:
            raise ValueError("Invalid or circular evidence index entry")
        require_digest(expected)
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"Changed or missing release evidence: {name}")


def hyperlight_payload(expected: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Preserve the existing payload checks after all release attestations have passed."""
    inputs_hash = require_digest(expected.get("buildInputsSha256"))
    index = read(directory / "evidence-index.json")
    if index.get("hyperlight-build-inputs.json") != inputs_hash:
        raise ValueError("Hyperlight build inputs differ from the independently selected policy")
    image = f"{PREFIX}/hyperlight@{expected['registryDigest']}"
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", image], check=True, timeout=600)
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    ).stdout.strip()
    if image_id != expected["imageId"]:
        raise ValueError("Pulled Hyperlight configuration differs from the verified manifest")
    smoke = verify_image(image_id, inputs_hash.removeprefix("sha256:"))
    source = smoke.get("source")
    if (
        not isinstance(source, dict)
        or source.get("repository") != SOURCE_URL
        or source.get("revision") != expected["sourceCommit"]
        or source.get("dirty") is not False
    ):
        raise ValueError("Hyperlight payload does not match the approved clean source")
    return smoke


def _verify(
    expected: dict[str, Any],
    directory: Path,
    *,
    bundles: bool = False,
    registry: bool = False,
    candidate: bool = False,
) -> dict[str, Any]:
    """Authenticate immutable release evidence; monitoring policy is reported separately."""
    validate_identity(expected)
    if bundles and registry:
        raise ValueError("Select one attestation retrieval source")
    if expected["profile"] == "hyperlight":
        require_digest(expected.get("buildInputsSha256"))
    manifest = directory / "manifest.json"
    if manifest.is_symlink() or digest(manifest) != expected["registryDigest"]:
        raise ValueError("Manifest differs from the selected registry digest")
    document = read(manifest)
    if (
        document.get("mediaType") != MANIFEST
        or document.get("config", {}).get("digest") != expected["imageId"]
        or expected["assessedManifestDigest"] != expected["registryDigest"]
    ):
        raise ValueError("Expected the assessed single image manifest")
    claims = {}
    predicates = [
        (PROVENANCE, "provenance.jsonl"),
        (SPDX, "sbom.jsonl"),
    ]
    if not candidate:
        predicates.append((COMPLETION, "completion.jsonl"))
    for predicate, filename in predicates:
        target = (
            f"oci://{PREFIX}/{expected['profile']}@{expected['registryDigest']}"
            if registry
            else str(manifest)
        )
        command = ["gh", "attestation", "verify", target, *policy_flags(expected, predicate)]
        if registry:
            command.append("--bundle-from-oci")
        if bundles:
            bundle = directory / filename
            if bundle.is_symlink() or not bundle.is_file() or not bundle.stat().st_size:
                raise ValueError("Missing original attestation bundle")
            command.extend(["--bundle", str(bundle)])
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=120,
        )
        claims[predicate] = statement(json.loads(result.stdout), expected, predicate)
    dependencies = claims[PROVENANCE].get("buildDefinition", {}).get("resolvedDependencies", [])
    source = {
        "uri": f"git+https://github.com/{REPOSITORY}@refs/heads/main",
        "digest": {"gitCommit": expected["sourceCommit"]},
    }
    if source not in dependencies:
        raise ValueError("Provenance does not resolve the selected payload source")
    spdx = claims[SPDX]
    if (
        spdx.get("spdxVersion") != "SPDX-2.3"
        or not isinstance(spdx.get("packages"), list)
        or not spdx["packages"]
    ):
        raise ValueError("SBOM attestation contains no SPDX component inventory")
    if not candidate:
        evidence_hash = validate_completion(claims[COMPLETION], expected)
        verify_evidence(directory, evidence_hash)
    if read(directory / "sbom.spdx.json") != spdx:
        raise ValueError("Retained SPDX inventory differs from the attestation")
    result = {
        "candidateIdentityVerified" if candidate else "releaseIdentityVerified": True,
        "profile": expected["profile"],
        "version": expected["version"],
        "digest": expected["registryDigest"],
        "monitoringStatus": "not-queried",
    }
    return result


def verify_candidate(expected: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Authenticate publisher-only qualification inputs without claiming release completion."""
    return _verify(expected, directory, bundles=True, candidate=True)


def verify_identity(
    expected: dict[str, Any],
    directory: Path,
    *,
    bundles: bool = False,
    registry: bool = False,
) -> dict[str, Any]:
    """Authenticate all three release attestations without executing image code."""
    return _verify(expected, directory, bundles=bundles, registry=registry)


def verify(
    expected: dict[str, Any],
    directory: Path,
    *,
    bundles: bool = False,
    registry: bool = False,
) -> dict[str, Any]:
    """Verify release identity and preserve Hyperlight's additional payload qualification."""
    result = verify_identity(expected, directory, bundles=bundles, registry=registry)
    if expected["profile"] == "hyperlight":
        result["hyperlightPayloadVerification"] = hyperlight_payload(expected, directory)
    return result


def main() -> None:
    """Verify release evidence and, for Hyperlight, its payload under independently selected policy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    retrieval = parser.add_mutually_exclusive_group()
    retrieval.add_argument(
        "--bundles", action="store_true", help="Use retained bundles instead of GitHub retrieval"
    )
    retrieval.add_argument(
        "--registry",
        action="store_true",
        help="Retrieve the attestations from the exact OCI digest",
    )
    args = parser.parse_args()
    expected = read(args.policy)
    result = verify(expected, args.evidence, bundles=args.bundles, registry=args.registry)
    result.update(report(expected))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
