"""Verify a published Hyperlight runtime's provenance before running its packaging check."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from build_hyperlight_aks_image import SOURCE_URL, verify_image
from hyperlight_evidence import sha, sidecar_path, validate_candidate, verify_bundle

REPOSITORY = "sokolaidev/maf-extensions"
PREDICATE = "https://slsa.dev/provenance/v1"
ISSUER = "https://token.actions.githubusercontent.com"


def _verify_published_image(
    image: str,
    *,
    signer_identity: str,
    source_revision: str,
    source_ref: str,
    build_inputs_sha256: str,
    output: Path,
) -> dict[str, object]:
    """Require host-selected provenance policy and payload hashes; retain success only at the end."""
    output.unlink(missing_ok=True)
    validate_candidate(
        {
            "image": image,
            "signer_identity": signer_identity,
            "source_revision": source_revision,
            "source_ref": source_ref,
            "build_inputs_sha256": build_inputs_sha256,
        }
    )
    result = subprocess.run(
        [
            "gh",
            "attestation",
            "verify",
            f"oci://{image}",
            "--hostname",
            "github.com",
            "--repo",
            REPOSITORY,
            "--cert-identity",
            signer_identity,
            "--cert-oidc-issuer",
            ISSUER,
            "--source-digest",
            source_revision,
            "--source-ref",
            source_ref,
            "--predicate-type",
            PREDICATE,
            "--deny-self-hosted-runners",
            "--format",
            "json",
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    attestations = json.loads(result.stdout)
    digest = image.rsplit("@sha256:", 1)[1]
    if not isinstance(attestations, list) or not attestations:
        raise ValueError("GitHub CLI returned no verified attestations")
    for attestation in attestations:
        if (
            not isinstance(attestation, dict)
            or not isinstance(attestation.get("attestation"), dict)
            or not isinstance(attestation["attestation"].get("bundle"), dict)
            or not attestation["attestation"]["bundle"]
            or not isinstance(attestation.get("verificationResult"), dict)
        ):
            raise ValueError("GitHub CLI returned an invalid verification result")
        statement = attestation["verificationResult"].get("statement")
        if (
            not isinstance(statement, dict)
            or statement.get("predicateType") != PREDICATE
            or not isinstance(statement.get("subject"), list)
            or not any(
                isinstance(subject, dict)
                and isinstance(subject.get("digest"), dict)
                and subject["digest"].get("sha256") == digest
                for subject in statement["subject"]
            )
        ):
            raise ValueError("verified statement does not describe the requested image digest")

    subprocess.run(["docker", "pull", "--platform", "linux/amd64", image], check=True)
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    smoke = verify_image(image_id, build_inputs_sha256)
    source = smoke.get("source")
    if (
        not isinstance(source, dict)
        or source.get("repository") != SOURCE_URL
        or source.get("revision") != source_revision
        or source.get("dirty") is not False
    ):
        raise ValueError("runtime payload does not match the approved clean source revision")
    record = {
        "schema_version": 1,
        "verified_at": datetime.now(UTC).isoformat(),
        "image": image,
        "local_image_id": image_id,
        "registry_digest": f"sha256:{digest}",
        "signed_provenance_verified": True,
        "policy": {
            "repository": REPOSITORY,
            "signer_identity": signer_identity,
            "oidc_issuer": ISSUER,
            "source_revision": source_revision,
            "source_ref": source_ref,
            "predicate_type": PREDICATE,
            "deny_self_hosted_runners": True,
            "build_inputs_sha256": build_inputs_sha256,
        },
        "attestations": attestations,
        "smoke": smoke,
    }
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        temporary_path = Path(temporary) / "verification.json"
        temporary_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary_path, output)
    return record


def verify_published_image(
    image: str,
    *,
    signer_identity: str,
    source_revision: str,
    source_ref: str,
    build_inputs_sha256: str,
    output: Path,
    trusted_root: Path | None = None,
) -> dict[str, object]:
    """Retain original bytes after online, payload and offline checks pass."""
    output = output.absolute()
    sidecar = sidecar_path(output)
    if trusted_root is not None and trusted_root.resolve() in {
        output.resolve(),
        sidecar.resolve(),
    }:
        raise ValueError("trust input and outputs must be different files")
    output.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)
    candidate = {
        "image": image,
        "signer_identity": signer_identity,
        "source_revision": source_revision,
        "source_ref": source_ref,
        "build_inputs_sha256": build_inputs_sha256,
    }
    try:
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            root = Path(temporary)
            staged = root / "verification.json"
            record = _verify_published_image(**candidate, output=staged)
            manifest = subprocess.run(
                ["docker", "buildx", "imagetools", "inspect", "--raw", image],
                check=True,
                stdout=subprocess.PIPE,
                timeout=60,
            ).stdout
            (root / "signed-manifest.json").write_bytes(manifest)
            if sha(root / "signed-manifest.json") != image.rsplit("@sha256:", 1)[1]:
                raise ValueError("registry manifest bytes differ from approved digest")
            subprocess.run(
                [
                    "gh",
                    "attestation",
                    "download",
                    str(root / "signed-manifest.json"),
                    "--hostname",
                    "github.com",
                    "--repo",
                    REPOSITORY,
                    "--predicate-type",
                    PREDICATE,
                ],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                timeout=60,
            )
            downloads = list(root.glob("sha256*.jsonl"))
            if len(downloads) != 1:
                raise ValueError("missing or ambiguous downloaded attestation bundle")
            downloads[0].rename(root / "attestation-bundles.jsonl")
            trust_bytes = (
                trusted_root.read_bytes()
                if trusted_root is not None
                else subprocess.run(
                    ["gh", "attestation", "trusted-root", "--hostname", "github.com"],
                    check=True,
                    stdout=subprocess.PIPE,
                    timeout=60,
                ).stdout
            )
            (root / "trusted-root.jsonl").write_bytes(trust_bytes)
            trust_hash = sha(root / "trusted-root.jsonl")
            verify_bundle(root, candidate, trust_hash)
            files = ("signed-manifest.json", "attestation-bundles.jsonl", "trusted-root.jsonl")
            with zipfile.ZipFile(root / "evidence.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                for name in files:
                    archive.write(root / name, name)
            record["retained_evidence"] = {
                "archive": sidecar.name,
                "archive_sha256": sha(root / "evidence.zip"),
                "files": {name: sha(root / name) for name in files},
                "trusted_root_sha256": trust_hash,
                "offline_verified": True,
            }
            staged.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(root / "evidence.zip", sidecar)
            os.replace(staged, output)
            return record
    except BaseException:
        output.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise


def main() -> None:
    """Verify a digest against explicit operator policy; do not publish or deploy it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Registry/repository@sha256:DIGEST")
    parser.add_argument(
        "--signer-identity", required=True, help="Exact GitHub workflow certificate identity"
    )
    parser.add_argument("--source-revision", required=True, help="Approved full source commit SHA")
    parser.add_argument(
        "--source-ref", required=True, help="Approved refs/heads/... or refs/tags/..."
    )
    parser.add_argument(
        "--build-inputs-sha256", required=True, help="Expected prepared build-inputs.json hash"
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Local provenance verification record"
    )
    parser.add_argument(
        "--trusted-root",
        type=Path,
        help="Operator-approved snapshot; otherwise use GitHub CLI authenticated TUF roots",
    )
    args = parser.parse_args()
    verify_published_image(
        args.image,
        signer_identity=args.signer_identity,
        source_revision=args.source_revision,
        source_ref=args.source_ref,
        build_inputs_sha256=args.build_inputs_sha256,
        output=args.output,
        trusted_root=args.trusted_root,
    )
    print(f"Verified published runtime; evidence: {args.output}")


if __name__ == "__main__":
    main()
