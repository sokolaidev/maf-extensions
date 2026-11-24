"""Collect and retain the signed upstream Hyperlight device-plugin build for downstream review."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from container_release import decode, digest, read, write
from container_release_assets import Evidence
from container_release_oci import INDEX, MANIFEST, blob
from hyperlight_evidence import ISSUER, PREDICATE

UPSTREAM = "hyperlight-dev/hyperlight-on-kubernetes"
SOURCE = "f78f93f2ca50844e7a794e4e547bd23e254329e5"
IMAGE = "ghcr.io/hyperlight-dev/hyperlight-device-plugin"
INDEX_DIGEST = "sha256:077997abe85debe7920ae42f7fd985ba6013a6854ce1a56d7a1adc45e1aa8c70"
AMD64_DIGEST = "sha256:e83d353338a643aedc351975df32d7951f2966988d21b7f2e0066da61485c4c6"
WORKFLOW = ".github/workflows/publish-device-plugin.yml"
SPDX = "https://spdx.dev/Document"
ARCHIVE = "hyperlight-device-plugin-evidence.zip"


def gh(directory: Path, *args: str) -> bytes:
    """Preserve GitHub CLI output bytes and leave failure diagnostics in the job log."""
    return subprocess.run(
        ["gh", *args], cwd=directory, check=True, stdout=subprocess.PIPE, timeout=120
    ).stdout


def collect_manifests(layout: Path, files: Path) -> None:
    """Follow only digest-checked descriptors from the reviewed index."""
    index = layout / "blobs" / "sha256" / INDEX_DIGEST.split(":")[1]
    if digest(index) != INDEX_DIGEST or read(index)["mediaType"] != INDEX:
        raise ValueError("Unexpected upstream image index")
    shutil.copyfile(index, files / "signed-manifest.json")
    descriptors = read(index)["manifests"]
    (platform,) = [
        d for d in descriptors if d.get("platform") == {"architecture": "amd64", "os": "linux"}
    ]
    if platform["digest"] != AMD64_DIGEST or platform["mediaType"] != MANIFEST:
        raise ValueError("Unexpected Linux/amd64 image")
    shutil.copyfile(blob(layout, platform), files / "linux-amd64.json")
    (attestation,) = [
        d
        for d in descriptors
        if d.get("annotations", {}).get("vnd.docker.reference.digest") == AMD64_DIGEST
        and d["annotations"].get("vnd.docker.reference.type") == "attestation-manifest"
    ]
    manifest = blob(layout, attestation)
    if read(manifest)["subject"]["digest"] != AMD64_DIGEST:
        raise ValueError("Attestation manifest identifies another image")
    shutil.copyfile(manifest, files / "attestation-manifest.json")
    for kind, name in [(PREDICATE, "buildkit-provenance.json"), (SPDX, "sbom.json")]:
        (layer,) = [
            d
            for d in read(manifest)["layers"]
            if d["annotations"].get("in-toto.io/predicate-type") == kind
        ]
        path = blob(layout, layer)
        statement = read(path)
        if statement["predicateType"] != kind or not any(
            s["digest"].get("sha256") == AMD64_DIGEST.split(":")[1] for s in statement["subject"]
        ):
            raise ValueError(f"Wrong subject or predicate in {name}")
        shutil.copyfile(path, files / name)


def verify_materials(files: Path) -> None:
    """Match both Dockerfile stages and the source revision to the BuildKit statement."""
    definition = read(files / "buildkit-provenance.json")["predicate"]["buildDefinition"]
    args = definition["externalParameters"]["request"]["root"]["request"]["args"]
    if (args.get("vcs:source"), args.get("vcs:revision")) != (
        f"https://github.com/{UPSTREAM}",
        SOURCE,
    ):
        raise ValueError("Unexpected BuildKit source")
    bases = [
        line
        for line in (files / "Dockerfile").read_text("utf-8").splitlines()
        if line.upper().startswith("FROM ")
    ]
    if len(bases) != 2:
        raise ValueError("Expected both upstream Dockerfile stages")
    for line, name in zip(bases, ("golang", "alpine"), strict=True):
        match = re.fullmatch(rf"FROM {name}:([\w.-]+)@sha256:([0-9a-f]{{64}})(?: AS \w+)?", line)
        if not match:
            raise ValueError(f"Expected a digest-pinned {name} base")
        tag, sha = match.groups()
        uri = f"pkg:docker/{name}@{tag}?digest=sha256:{sha}&platform=linux%2Famd64"
        if not any(
            d["uri"] == uri and d["digest"] == {"sha256": sha}
            for d in definition["resolvedDependencies"]
        ):
            raise ValueError(f"BuildKit materials differ from the {name} base")


def verify_signature(files: Path) -> None:
    """Verify the retained index using the original signature bundle and trust roots."""
    result = gh(
        files,
        "attestation",
        "verify",
        "signed-manifest.json",
        "--bundle",
        "attestation-bundles.jsonl",
        "--custom-trusted-root",
        "trusted-root.jsonl",
        "--repo",
        UPSTREAM,
        "--hostname",
        "github.com",
        "--cert-identity",
        f"https://github.com/{UPSTREAM}/{WORKFLOW}@refs/heads/main",
        "--cert-oidc-issuer",
        ISSUER,
        "--source-ref",
        "refs/heads/main",
        "--source-digest",
        SOURCE,
        "--deny-self-hosted-runners",
        "--predicate-type",
        PREDICATE,
        "--format",
        "json",
    )
    subject = {"name": IMAGE, "digest": {"sha256": INDEX_DIGEST.split(":")[1]}}
    if not any(
        subject in record["verificationResult"]["statement"]["subject"] for record in decode(result)
    ):
        raise ValueError("Signed provenance identifies another image")
    (files / "verification.json").write_bytes(result)


def collect(output: Path) -> None:
    """Build a portable evidence archive from the reviewed upstream candidate."""
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        layout = Path(temp, "oci")
        files = Path(temp, "evidence")
        files.mkdir()
        subprocess.run(
            [
                "skopeo",
                "copy",
                "--all",
                "--preserve-digests",
                f"docker://{IMAGE}@{INDEX_DIGEST}",
                f"oci:{layout}:plugin",
            ],
            check=True,
            timeout=420,
        )
        collect_manifests(layout, files)
        for source, name in [
            (WORKFLOW, "workflow.yml"),
            ("device-plugin/Dockerfile", "Dockerfile"),
            ("deploy/manifests/device-plugin.yaml", "device-plugin.yaml"),
        ]:
            (files / name).write_bytes(
                gh(
                    files,
                    "api",
                    "-H",
                    "Accept: application/vnd.github.raw+json",
                    f"repos/{UPSTREAM}/contents/{source}?ref={SOURCE}",
                )
            )
        verify_materials(files)
        gh(
            files,
            "attestation",
            "download",
            "signed-manifest.json",
            "--repo",
            UPSTREAM,
            "--predicate-type",
            PREDICATE,
        )
        (bundle,) = files.glob("sha256*.jsonl")
        bundle.rename(files / "attestation-bundles.jsonl")
        (files / "trusted-root.jsonl").write_bytes(gh(files, "attestation", "trusted-root"))
        verify_signature(files)
        write(
            files / "candidate.json",
            {"image": f"{IMAGE}@{INDEX_DIGEST}", "source": SOURCE, "platformDigest": AMD64_DIGEST},
        )
        write(files / "checksums.json", {p.name: digest(p) for p in files.iterdir()})
        with zipfile.ZipFile(output / ARCHIVE, "w", zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(files.iterdir()):
                archive.write(p, p.name)


def publish(directory: Path, expected: str) -> dict[str, Any]:
    """Retain the archive for this verification attempt and confirm its immutability."""
    if digest(directory / ARCHIVE) != expected:
        raise ValueError("Transferred evidence differs from the verified archive")
    source = os.environ["GITHUB_SHA"]
    tag = f"image-hyperlight-device-plugin-{INDEX_DIGEST.split(':')[1]}-{os.environ['GITHUB_RUN_ID']}-{os.environ['EVIDENCE_ATTEMPT']}"
    evidence = Evidence()
    release = evidence.ensure(tag, source)
    evidence.retain(release, directory, {ARCHIVE: expected})
    if [asset["name"] for asset in evidence.github.assets(release["id"])] != [ARCHIVE]:
        raise ValueError("Unexpected assets in plugin evidence release")
    return evidence.publish(release, source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "publish"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-digest")
    options = parser.parse_args()
    if options.action == "collect":
        collect(options.directory)
    elif options.expected_digest:
        release = publish(options.directory, options.expected_digest)
        print(
            json.dumps({"release": release["html_url"], "archiveDigest": options.expected_digest})
        )
    else:
        parser.error("publish requires --expected-digest")
