"""Compare dependencies consumed by image construction and offline qualification."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

# Include host qualification dependencies as well as packages embedded in an image.
ROOTS = {
    "antivirus": ("maf-sandbox-antivirus", "maf-sandbox-docker"),
    "bicep": (),
    "bicep-avm": (),
    "graphviz": (),
    "terraform-random": (),
    "terraform-avm": (),
    "opentofu-random": (),
    "opentofu-avm": (),
    "azure-pricing": ("maf-sandbox-azure-pricing", "maf-sandbox-docker"),
    "bicep-docker-sbx": ("maf-sandbox-docker-sbx",),
    "document": ("maf-sandbox-document", "maf-sandbox-docker", "python-pptx"),
    "drawio": ("maf-sandbox-drawio", "maf-sandbox-docker", "pillow"),
    "drawio-export": ("maf-sandbox-drawio", "maf-sandbox-docker", "pillow"),
    "shell": ("maf-sandbox-shell", "maf-sandbox-docker"),
    "egress-proxy": ("maf-sandbox-docker", "cryptography"),
    "hyperlight-aks-probe": ("maf-sandbox", "maf-sandbox-codeact", "maf-sandbox-hyperlight"),
}


def document(root: Path, revision: str, path: str) -> dict[str, Any]:
    """Read dependency metadata from the exact side of the Git comparison."""
    raw = subprocess.check_output(
        ["git", "show", f"{revision}:{path}"],
        cwd=root,
        text=True,
        encoding="utf-8",
        stderr=subprocess.PIPE,
    )
    return tomllib.loads(raw)


def normalize(name: str) -> str:
    """Use Python distribution-name equivalence for dependency edges."""
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(lock: dict[str, Any], roots: tuple[str, ...]) -> dict[str, str]:
    """Include all marker/version alternatives and extras rather than guessing the runner."""
    packages: dict[str, list[dict[str, Any]]] = {}
    for package in lock["package"]:
        packages.setdefault(normalize(package["name"]), []).append(package)
    remaining = list(roots)
    result = {}
    while remaining:
        name = normalize(remaining.pop())
        if name in result:
            continue
        variants = packages[name]
        result[name] = json.dumps(variants, sort_keys=True)
        for variant in variants:
            dependencies = list(variant.get("dependencies", []))
            for kind in ("optional-dependencies", "dev-dependencies"):
                for entries in variant.get(kind, {}).values():
                    dependencies.extend(entries)
            remaining.extend(edge["name"] for edge in dependencies)
    return result


def affected_dependencies(
    root: Path,
    base: str,
    head: str,
    paths: set[str],
    profiles: tuple[str, ...],
) -> set[str]:
    """Select consumers of changed lock nodes or package metadata; malformed input raises."""
    before, after = (document(root, revision, "uv.lock") for revision in (base, head))
    # Manifest membership is workspace bookkeeping; resolution policy remains shared.
    if {k: v for k, v in before.items() if k not in {"package", "manifest"}} != {
        k: v for k, v in after.items() if k not in {"package", "manifest"}
    }:
        return set(profiles)
    if "pyproject.toml" in paths:
        metadata = [document(root, revision, "pyproject.toml") for revision in (base, head)]
        for item in metadata:
            item.pop("dependency-groups", None)
        if metadata[0] != metadata[1]:
            return set(profiles)
    changed_packages = set()
    for path in paths:
        if path.startswith("packages/") and path.endswith("/pyproject.toml"):
            old, new = (document(root, revision, path)["project"] for revision in (base, head))
            if old["name"] != new["name"]:
                return set(profiles)
            changed_packages.add(normalize(new["name"]))
    selected = set()
    for profile in profiles:
        roots = (*ROOTS[profile], "pyyaml")
        previous, current = (closure(lock, roots) for lock in (before, after))
        if previous != current or changed_packages.intersection(previous.keys() | current.keys()):
            selected.add(profile)
    return selected
