"""Bind consumer compatibility to published image bytes independently of image releases."""

from __future__ import annotations

import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from container_release import ISSUER, REPOSITORY, decode, require_digest, timestamp, write
from container_release_history import encode, sha256
from select_image_security import IMAGE_INPUTS

WORKFLOW = ".github/workflows/kind-consumer-qualification.yml"
SIGNER = f"https://github.com/{REPOSITORY}/{WORKFLOW}@refs/heads/main"
PREDICATE = f"https://github.com/{REPOSITORY}/consumer-qualification/v1"
SUBJECT = "consumer-inputs.json"
KIND_IMAGES = {
    "maf-sandbox-azure-pricing": ("azure-pricing",),
    "maf-sandbox-document": ("document", "document-export"),
    "maf-sandbox-antivirus": ("antivirus",),
    "maf-sandbox-bicep": ("bicep", "bicep-avm"),
    "maf-sandbox-drawio": ("drawio", "drawio-export"),
    "maf-sandbox-shell": ("shell",),
    "maf-sandbox-terraform": ("terraform-random", "opentofu-random", "terraform-avm"),
}
# Coverage is a release requirement, not an assertion of support on other backends.
BACKENDS = {profile: ("docker",) for profiles in KIND_IMAGES.values() for profile in profiles}
HARNESS_INPUTS = {"drawio-export": ("images/drawio-sandbox/DEJAVU-LICENSE",)}


def git(root: Path, *args: str) -> str:
    """Read committed inputs without depending on a complete working tree."""
    return subprocess.check_output(
        ["git", *args], cwd=root, encoding="utf-8", stderr=subprocess.PIPE
    ).strip()


def inputs(
    root: Path, head: str, package: str, profile: str, backend: str, candidate: dict[str, Any]
) -> dict[str, Any]:
    """Fingerprint consumer source, locked dependencies, harness and execution policy."""
    if profile not in KIND_IMAGES.get(package, ()) or backend not in BACKENDS[profile]:
        raise ValueError("Unknown consumer qualification scope")
    files = set(git(root, "ls-tree", "-r", "--name-only", head).splitlines())
    selected: dict[str, str] = {}
    packages = (package, "maf-sandbox", f"maf-sandbox-{backend}")
    for name in packages:
        path = f"packages/{name}/src"
        selected[path] = git(root, "rev-parse", f"{head}:{path}")
    for path in (
        "scripts",
        "tests/fixtures",
        ".github/actions/setup-classic-docker",
        WORKFLOW,
        "pyproject.toml",
        "uv.toml",
        ".python-version",
    ):
        if path in files or any(p.startswith(path + "/") for p in files):
            selected[path] = git(root, "rev-parse", f"{head}:{path}")
    for prefix, profiles in IMAGE_INPUTS.items():
        if profile in profiles and prefix.startswith("images/"):
            path = prefix.rstrip("/")
            if any(p.startswith(prefix) for p in files):
                selected[path] = git(root, "rev-parse", f"{head}:{path}")
    for path in HARNESS_INPUTS.get(profile, ()):
        selected[path] = git(root, "rev-parse", f"{head}:{path}")
    versions = {}
    for path in sorted(files):
        if re.fullmatch(r"packages/[^/]+/pyproject\.toml", path):
            metadata = tomllib.loads(git(root, "show", f"{head}:{path}"))
            version = metadata["project"].pop("version")
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("Unsupported workspace version normalization")
            versions[path.removesuffix("/pyproject.toml")] = (metadata["project"]["name"], version)
            selected[path] = sha256(encode(metadata))
    lock = tomllib.loads(git(root, "show", f"{head}:uv.lock"))
    for entry in lock.get("package", []):
        source = entry.get("source", {})
        path = source.get("editable")
        if path in versions and source == {"editable": path}:
            if (entry.get("name"), entry.get("version")) != versions[path]:
                raise ValueError("Workspace metadata and lock version disagree")
            entry["version"] = "workspace"
    selected["uv.lock"] = sha256(encode(lock))
    return {
        "schemaVersion": 1,
        "package": package,
        "profile": profile,
        "backend": backend,
        "platform": "linux/amd64",
        "registryDigest": require_digest(candidate["registryDigest"]),
        "imageId": require_digest(candidate["imageId"]),
        "inputs": selected,
    }


def validate_runtime(value: dict[str, Any], expected: dict[str, Any]) -> None:
    """Require successful checks for the selected bytes and explicit resource cleanup."""
    if (
        value.get("passed") is not True
        or value.get("profile") != expected["profile"]
        or value.get("imageId") != expected["imageId"]
        or value.get("containersRemaining") != []
    ):
        raise ValueError("Consumer runtime failed, identifies other bytes, or lacks cleanup")
    for detail in value.values():
        if isinstance(detail, dict):
            for key in ("containersRemaining", "networksRemaining", "sandboxesRemaining"):
                if key in detail and detail[key] != []:
                    raise ValueError("Consumer runtime left resources behind")
    if expected["profile"] == "azure-pricing":
        pricing = value.get("azurePricing", {})
        if (
            pricing.get("backend") != "docker"
            or pricing.get("imageId") != expected["imageId"]
            or pricing.get("livePricingVerified") is not True
            or pricing.get("containersRemaining") != []
            or pricing.get("networksRemaining") != []
        ):
            raise ValueError("Pricing qualification lacks Docker execution and cleanup")
        require_digest(pricing.get("proxyImageId"))
        checks = pricing.get("checks", [])
        if not all(
            any(c.get("probe") == probe and c.get("denied") is True for c in checks)
            for probe in ("unlisted-host", "post-method")
        ) or not all(
            any(
                c.get("priceType") == category
                and c.get("checkedPricesIntegrity") == "trusted"
                and c.get("reportIntegrity") == "untrusted"
                and type(c.get("rows")) is int
                and c["rows"] > 0
                for c in checks
            )
            for category in ("Reservation", "Consumption")
        ):
            raise ValueError("Pricing qualification lacks pricing or enforcement checks")


def validate_record(value: dict[str, Any], expected: dict[str, Any]) -> None:
    """Validate the signed claim separately from its signer and subject authentication."""
    if (
        value.get("schemaVersion") != 1
        or value.get("inputs") != expected
        or value.get("anonymousPullVerified") is not True
        or not re.fullmatch(r"[0-9a-f]{40}", value.get("sourceCommit", ""))
        or not re.fullmatch(r"[1-9][0-9]*", value.get("runId", ""))
        or not re.fullmatch(r"[1-9][0-9]*", value.get("runAttempt", ""))
        or not value.get("environment", {}).get("distributions")
        or not value.get("environment", {}).get("python")
        or not value.get("environment", {}).get("docker")
    ):
        raise ValueError("Consumer qualification has incomplete or mismatched identity")
    timestamp(value.get("checkedAt"))
    validate_runtime(value.get("runtime", {}), expected)


def verify(expected: dict[str, Any]) -> dict[str, Any]:
    """Retrieve signed claims by input digest, independent of Actions artifact retention."""
    with tempfile.TemporaryDirectory(prefix="kind-consumer-") as temporary:
        subject = Path(temporary) / SUBJECT
        write(subject, expected)
        command = [
            "gh",
            "attestation",
            "verify",
            str(subject),
            "--repo",
            REPOSITORY,
            "--cert-identity",
            SIGNER,
            "--cert-oidc-issuer",
            ISSUER,
            "--source-ref",
            "refs/heads/main",
            "--deny-self-hosted-runners",
            "--predicate-type",
            PREDICATE,
            "--format",
            "json",
        ]
        try:
            result = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=120
            )
        except subprocess.SubprocessError as error:
            raise ValueError(
                "Matching authenticated consumer qualification unavailable; "
                f"run {WORKFLOW} on main for {expected['profile']}"
            ) from error
        values = decode(result.stdout)
        if not isinstance(values, list) or not values:
            raise ValueError("No authenticated consumer qualification returned")
        for item in values:
            statement = item.get("verificationResult", {}).get("statement", {})
            if (
                statement.get("_type") != "https://in-toto.io/Statement/v1"
                or statement.get("predicateType") != PREDICATE
                or statement.get("subject")
                != [
                    {
                        "name": SUBJECT,
                        "digest": {"sha256": sha256(subject.read_bytes()).removeprefix("sha256:")},
                    }
                ]
            ):
                raise ValueError("Consumer attestation subject or predicate differs")
            claim = statement.get("predicate", {})
            validate_record(claim, expected)
            # Bind the recorded checkout to the certificate, not just signed prose.
            pinned = [
                *command,
                "--source-digest",
                claim["sourceCommit"],
                "--signer-digest",
                claim["sourceCommit"],
            ]
            verified = decode(
                subprocess.run(
                    pinned, check=True, capture_output=True, text=True, timeout=120
                ).stdout
            )
            if any(
                row.get("verificationResult", {}).get("statement") == statement for row in verified
            ):
                return claim
        raise ValueError("Consumer qualification source differs from its signer")
