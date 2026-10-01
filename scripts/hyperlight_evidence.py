"""Shared byte-preserving storage and offline signature checks for operator evidence."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Any

REPOSITORY = "sokolaidev/maf-extensions"
PREDICATE = "https://slsa.dev/provenance/v1"
ISSUER = "https://token.actions.githubusercontent.com"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def sha(path: Path) -> str:
    """Hash exact file bytes."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path) -> Any:
    """Read operator JSON, allowing a Windows UTF-8 BOM."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def require_hash(value: object) -> str:
    """Require a canonical SHA-256 value from the operator."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("expected hash must be a lowercase SHA-256 digest")
    return value


def extract_archive(archive: Path, destination: Path, expected_hash: object) -> None:
    """Authenticate a bounded ZIP before extracting unique, flat, regular evidence files."""
    expected = require_hash(expected_hash)
    # Read once so a replacement between hashing and extraction cannot change the bytes.
    with archive.open("rb") as stream:
        data = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("archive hash mismatch or archive exceeds size limit")
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        members = source.infolist()
        names: set[str] = set()
        if len(members) > 128 or sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES:
            raise ValueError("archive exceeds extraction limits")
        for member in members:
            name = member.filename
            mode = member.external_attr >> 16
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
                or member.orig_filename != name
                or name.endswith(".")
                or name.split(".")[0].upper()
                in {
                    "CON",
                    "PRN",
                    "AUX",
                    "NUL",
                    *(f"COM{i}" for i in range(10)),
                    *(f"LPT{i}" for i in range(10)),
                }
                or name.casefold() in names
                or member.is_dir()
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or member.flag_bits & 1
            ):
                raise ValueError("unsafe or duplicate archive member")
            names.add(name.casefold())
        for member in members:
            (destination / member.filename).write_bytes(source.read(member))


def validate_candidate(candidate: dict[str, Any]) -> None:
    """Refuse incomplete expectations that would weaken GitHub CLI policy flags."""
    fields = ("image", "signer_identity", "source_revision", "source_ref", "build_inputs_sha256")
    if any(not isinstance(candidate.get(field), str) for field in fields):
        raise ValueError("candidate policy requires five string fields")
    image, signer_identity, source_revision, source_ref, build_inputs_sha256 = (
        candidate[field] for field in fields
    )
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


def policy_flags(candidate: dict[str, Any]) -> list[str]:
    """Use repository trust anchors and independently selected candidate expectations."""
    return [
        "--hostname",
        "github.com",
        "--repo",
        REPOSITORY,
        "--cert-identity",
        candidate["signer_identity"],
        "--cert-oidc-issuer",
        ISSUER,
        "--source-digest",
        candidate["source_revision"],
        "--source-ref",
        candidate["source_ref"],
        "--predicate-type",
        PREDICATE,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]


def verify_bundle(root: Path, candidate: dict[str, Any], trusted_root_sha256: object) -> list[Any]:
    """Verify local bytes without GitHub, registry or TUF lookups; policy is operator-supplied."""
    validate_candidate(candidate)
    digest = require_hash(candidate["image"].rsplit("@sha256:", 1)[-1])
    if sha(root / "signed-manifest.json") != digest:
        raise ValueError("signed manifest differs from expected image digest")
    if sha(root / "trusted-root.jsonl") != require_hash(trusted_root_sha256):
        raise ValueError("trust root differs from operator-approved snapshot")
    bundle = root / "attestation-bundles.jsonl"
    if not bundle.is_file() or not bundle.stat().st_size:
        raise ValueError("missing or empty original attestation bundle")
    result = subprocess.run(
        [
            "gh",
            "attestation",
            "verify",
            str(root / "signed-manifest.json"),
            *policy_flags(candidate),
            "--bundle",
            str(bundle),
            "--custom-trusted-root",
            str(root / "trusted-root.jsonl"),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    attestations = json.loads(result.stdout)
    if not isinstance(attestations, list) or not attestations:
        raise ValueError("no offline verified attestations")
    for item in attestations:
        statement = item["verificationResult"]["statement"]
        if statement.get("predicateType") != PREDICATE or not any(
            subject.get("digest", {}).get("sha256") == digest for subject in statement["subject"]
        ):
            raise ValueError("verified subject differs from expected image")
    return attestations


def sidecar_path(output: Path) -> Path:
    """Return the reserved byte-evidence sibling of a report."""
    return output.with_name(output.name + ".evidence.zip")
