"""Validate an OCI layout before promoting its exact manifests and blobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from container_release import digest, read, require_digest

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
LAYERS = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd",
}


def blob(root: Path, descriptor: dict[str, Any]) -> Path:
    """Refuse a missing, substituted or linked content-addressed object."""
    expected = require_digest(descriptor.get("digest"))
    size = descriptor.get("size")
    if type(size) is not int or size < 0:
        raise ValueError("Invalid OCI descriptor size")
    path = root / "blobs" / "sha256" / expected.removeprefix("sha256:")
    if any(p.is_symlink() for p in (root, root / "blobs", path.parent, path)) or not path.is_file():
        raise ValueError("OCI object must be a regular retained file")
    if path.stat().st_size != size or digest(path) != expected:
        raise ValueError("OCI object differs from its descriptor")
    if descriptor.get("urls"):
        raise ValueError("External OCI blob URLs are not release inputs")
    return path


def manifest(root: Path, descriptor: dict[str, Any], image_id: str) -> dict[str, Any]:
    """Bind a runnable Linux/amd64 manifest to the assessed image configuration."""
    if descriptor.get("mediaType") != MANIFEST:
        raise ValueError("Expected an OCI image manifest")
    path = blob(root, descriptor)
    document = read(path)
    if (
        type(document.get("schemaVersion")) is not int
        or document["schemaVersion"] != 2
        or document.get("mediaType") != MANIFEST
        or document.get("artifactType")
        or document.get("subject")
    ):
        raise ValueError("Invalid runnable OCI manifest")
    config = document["config"]
    if config.get("mediaType") != CONFIG or config.get("digest") != image_id:
        raise ValueError("Manifest does not contain the assessed configuration")
    configuration = read(blob(root, config))
    if (configuration.get("os"), configuration.get("architecture")) != ("linux", "amd64"):
        raise ValueError("Release platform must be linux/amd64")
    layers = document.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("Expected image filesystem layers")
    for layer in layers:
        if layer.get("mediaType") not in LAYERS:
            raise ValueError("Unsupported image layer type")
        blob(root, layer)
    return document


def validate_layout(root: Path, image_id: str) -> dict[str, str]:
    """Accept one runnable manifest, with no unassessed sibling or nested index."""
    require_digest(image_id)
    if root.is_symlink() or any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("OCI layouts must not contain symlinks")
    if read(root / "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("Unsupported OCI layout")
    index = read(root / "index.json")
    if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
        raise ValueError("Expected exactly one retained image reference")
    descriptor = index["manifests"][0]
    # The publisher emits a single manifest; index inputs need separate evidence qualification.
    manifest(root, descriptor, image_id)
    return {
        "registryDigest": descriptor["digest"],
        "assessedManifestDigest": descriptor["digest"],
        "imageId": image_id,
    }


def verify_registry_manifest(data: bytes, expected: dict[str, str]) -> None:
    """Require registry bytes to match the retained, assessed single manifest."""
    import hashlib

    if "sha256:" + hashlib.sha256(data).hexdigest() != expected["registryDigest"]:
        raise ValueError("Registry changed the promoted manifest")
    document = json.loads(data)
    if (
        document.get("mediaType") != MANIFEST
        or document.get("config", {}).get("digest") != expected["imageId"]
    ):
        raise ValueError("Registry manifest does not identify the assessed configuration")
