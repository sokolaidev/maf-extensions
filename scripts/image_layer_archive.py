"""Check saved filesystem layers for entries ACAS cannot import."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

ACAS_PROFILES = frozenset(
    {
        "antivirus",
        "azure-pricing",
        "bicep",
        "bicep-avm",
        "document",
        "drawio",
        "drawio-export",
        "graphviz",
        "shell",
        "terraform-random",
        "opentofu-random",
        "terraform-avm",
        "opentofu-avm",
    }
)


def check_layer_archive(path: Path) -> int:
    """Reject special files in every saved layer, including hidden lower layers."""
    with tarfile.open(path) as archive:
        manifest_file = archive.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError("Missing Docker save manifest")
        manifests = json.load(manifest_file)
        if not isinstance(manifests, list) or len(manifests) != 1:
            raise ValueError("Expected one saved image")
        layers = manifests[0]["Layers"]
        if not isinstance(layers, list) or not layers:
            raise ValueError("Expected image filesystem layers")
        for name in layers:
            layer_file = archive.extractfile(name)
            if layer_file is None:
                raise ValueError("Missing image layer")
            with tarfile.open(fileobj=layer_file, mode="r|*") as layer:
                for member in layer:
                    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                        raise ValueError(
                            f"Image layer {name!r} contains a special file: {member.name!r}"
                        )
    return len(layers)
