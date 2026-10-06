"""Inspect fixed GHCR destinations and promote retained OCI bytes without rebuilding."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from container_release import (
    PREFIX,
    REPOSITORY,
    decode,
    digest,
    key,
    now,
    profiles,
    read,
    require_digest,
    reserve,
    scan_result,
    timestamp,
    validate_identity,
    version,
    write,
)
from container_release_history import History
from container_release_oci import INDEX, MANIFEST, validate_layout, verify_registry_manifest
from prepare_container_release import assess, run

LIMIT = 8 * 1024 * 1024
MANIFEST_TYPES = ", ".join(
    (
        MANIFEST,
        INDEX,
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward registry credentials to a redirected endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url: str, headers: dict[str, str]) -> bytes:
    """Read a bounded response from GHCR with normal TLS validation."""
    if urllib.parse.urlsplit(url).hostname != "ghcr.io" or not url.startswith("https://"):
        raise ValueError("Only the approved GHCR endpoint is supported")
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
        result = response.read(LIMIT + 1)
    if len(result) > LIMIT:
        raise ValueError("Registry response exceeds its size limit")
    return result


def manifest(profile: str, reference: str, *, authenticated: bool = False) -> bytes | None:
    """Only an explicit MANIFEST_UNKNOWN response establishes a missing tag or digest."""
    if profile not in profiles():
        raise ValueError("Unknown image profile")
    if reference.startswith("sha256:"):
        require_digest(reference)
    else:
        version(reference)
    headers = {}
    if authenticated:
        credentials = f"{os.environ['GITHUB_ACTOR']}:{os.environ['GH_TOKEN']}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
    scope = f"repository:{REPOSITORY}/{profile}:" + ("pull,push" if authenticated else "pull")
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": scope})
    token = decode(request(f"https://ghcr.io/token?{query}", headers)).get("token")
    if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
        raise ValueError("GHCR did not return a valid scoped token")
    try:
        return request(
            f"https://ghcr.io/v2/{REPOSITORY}/{profile}/manifests/{reference}",
            {"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES},
        )
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        document = decode(error.read(LIMIT + 1))
        errors = document.get("errors")
        if (
            not isinstance(errors, list)
            or not errors
            or any(item.get("code") != "MANIFEST_UNKNOWN" for item in errors)
        ):
            raise ValueError("Registry absence could not be confirmed") from error
        return None


def fresh_assessment(directory: Path, candidate: dict[str, Any]) -> None:
    """Refresh an expired approval-time assessment against the same retained image bytes."""
    assessment = read(directory / "assessment.json")
    result = scan_result(
        read(directory / "grype.json"), candidate["imageId"], assessment["assessedAt"]
    )
    if result != assessment or result["outcome"] != "clean":
        raise ValueError("Retained assessment is not a complete passing scan")
    age = timestamp(now()) - timestamp(assessment["assessedAt"])
    if age < timedelta(0):
        raise ValueError("Candidate assessment is dated in the future")
    if age >= timedelta(hours=24):
        for name in (
            "assessment.json",
            "grype.json",
            "grype.yaml",
            "sbom.syft.json",
            "sbom.spdx.json",
        ):
            preserved = directory / f"approval-{name}"
            if not preserved.exists():
                shutil.copyfile(directory / name, preserved)
        assess(directory, candidate["imageId"])


def validate_preparation(directory: Path, candidate: dict[str, Any]) -> None:
    """Bind approval-time reports and runtime results to the selected candidate envelope."""
    index = directory / "preparation-index.json"
    if index.is_symlink() or digest(index) != require_digest(
        candidate.get("preparationEvidenceSha256")
    ):
        raise ValueError("Preparation index differs from the approved candidate")
    files = read(index)
    required = {
        "manifest.json",
        "build.json",
        "runtime.json",
        "assessment.json",
        "grype.json",
        "grype.yaml",
        "sbom.syft.json",
        "sbom.spdx.json",
        "build-inputs.json",
    }
    if not isinstance(files, dict) or not required <= files.keys() or len(files) > 128:
        raise ValueError("Preparation evidence is incomplete")
    for name, expected in files.items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
            raise ValueError("Unsafe preparation evidence name")
        path = directory / name
        if path.is_symlink() or digest(path) != require_digest(expected):
            raise ValueError("Preparation evidence changed after assessment")
    actual = {p.name for p in directory.iterdir() if p.is_file()}
    if actual - files.keys() - {"candidate.json", "preparation-index.json", "approval.json"}:
        raise ValueError("Unexpected files in the retained preparation evidence")


def promote(directory: Path, history: History | None = None) -> dict[str, Any]:
    """Require a durable reservation and fresh scan before the first registry write."""
    candidate = read(directory / "candidate.json")
    validate_identity(candidate)
    validate_preparation(directory, candidate)
    layout = directory / "oci"
    identity = validate_layout(layout, candidate["imageId"])
    if any(candidate[field] != value for field, value in identity.items()):
        raise ValueError("Retained bytes differ from the reserved candidate")
    verified_manifest = (directory / "manifest.json").read_bytes()
    verify_registry_manifest(verified_manifest, candidate)
    run(["skopeo", "copy", f"oci:{layout}:candidate", "docker-daemon:maf-release:retained"])
    inspect = json.loads(
        run(["docker", "image", "inspect", "maf-release:retained"], capture_output=True).stdout
    )
    if inspect[0]["Id"] != candidate["imageId"]:
        raise ValueError("Loaded image differs from retained candidate")
    fresh_assessment(directory, candidate)
    latest = (history or History()).head()
    if latest is None or key(candidate) not in latest.catalogue["releases"]:
        raise ValueError("Registry writes require a durable version reservation")
    reserve(latest.catalogue, candidate, now())
    if latest.catalogue["releases"][key(candidate)]["state"] != "incomplete":
        raise ValueError("Completed releases permit evidence delivery only")
    existing = manifest(candidate["profile"], candidate["version"], authenticated=True)
    if existing is not None:
        verify_registry_manifest(existing, candidate)
    else:
        # Registry inspection and lock waits consume the same publication freshness budget.
        fresh_assessment(directory, candidate)
        with tempfile.TemporaryDirectory(prefix="maf-registry-auth-") as temporary:
            auth = Path(temporary) / "auth.json"
            credentials = f"{os.environ['GITHUB_ACTOR']}:{os.environ['GH_TOKEN']}".encode()
            write(auth, {"auths": {"ghcr.io": {"auth": base64.b64encode(credentials).decode()}}})
            auth.chmod(0o600)
            run(
                [
                    "skopeo",
                    "copy",
                    "--preserve-digests",
                    "--dest-authfile",
                    str(auth),
                    f"oci:{layout}:candidate",
                    f"docker://{PREFIX}/{candidate['profile']}:{candidate['version']}",
                ]
            )
    published = manifest(candidate["profile"], candidate["registryDigest"], authenticated=True)
    if published is None:
        raise ValueError("Promoted digest is not available from the registry")
    verify_registry_manifest(published, candidate)
    tagged = manifest(candidate["profile"], candidate["version"], authenticated=True)
    if tagged is None:
        raise ValueError("Promoted version tag is unavailable")
    verify_registry_manifest(tagged, candidate)
    return candidate


def anonymous_pull(candidate: dict[str, Any]) -> str:
    """Prove public manifest and image access separately from authenticated publication."""
    validate_identity(candidate)
    public = manifest(candidate["profile"], candidate["registryDigest"])
    if public is None:
        raise ValueError("Published digest is not publicly available")
    verify_registry_manifest(public, candidate)
    image = f"{PREFIX}/{candidate['profile']}@{candidate['registryDigest']}"
    with tempfile.TemporaryDirectory(prefix="maf-anonymous-pull-") as temporary:
        subprocess.run(
            ["docker", "--config", temporary, "pull", "--platform", "linux/amd64", image],
            check=True,
            timeout=900,
        )
    image_id = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True
    ).stdout.strip()
    if image_id != candidate["imageId"]:
        raise ValueError("Public image configuration differs from the assessed image")
    return image_id
