"""Verify a published Hyperlight runtime's provenance before running its packaging check."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from build_hyperlight_aks_image import SOURCE_URL, verify_image

REPOSITORY = "sokolaidev/maf-extensions"
PREDICATE = "https://slsa.dev/provenance/v1"
ISSUER = "https://token.actions.githubusercontent.com"


def verify_published_image(
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
    component = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    if not re.fullmatch(
        rf"{component}(?::[0-9]+)?/{component}(?:/{component})*@sha256:[0-9a-f]{{64}}",
        image,
    ):
        raise ValueError(
            "image must name an explicit registry/repository and SHA-256 digest, without a tag"
        )
    registry = image.split("/", 1)[0]
    if "." not in registry and ":" not in registry and registry != "localhost":
        raise ValueError("image must name an explicit registry, not a Docker Hub shorthand")
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("source revision must be a full Git commit SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", build_inputs_sha256):
        raise ValueError("build inputs must have a SHA-256 digest")
    if not re.fullmatch(r"refs/(?:heads|tags)/[^\s@]+", source_ref):
        raise ValueError("source ref must be an explicit branch or tag ref")
    if not re.fullmatch(
        r"https://github\.com/[\w.-]+/[\w.-]+/\.github/workflows/[\w.-]+\.ya?ml@(?:refs/(?:heads|tags)/[^\s@]+|[0-9a-f]{40})",
        signer_identity,
    ):
        raise ValueError("signer identity must name an exact GitHub workflow and ref")
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
    args = parser.parse_args()
    try:
        verify_published_image(
            args.image,
            signer_identity=args.signer_identity,
            source_revision=args.source_revision,
            source_ref=args.source_ref,
            build_inputs_sha256=args.build_inputs_sha256,
            output=args.output,
        )
    except subprocess.CalledProcessError as error:
        if error.stdout:
            print(error.stdout, file=sys.stderr)
        raise
    print(f"Verified published runtime; evidence: {args.output}")


if __name__ == "__main__":
    main()
