"""Expose the vendor Chrome package to NVD matching without dropping its OS inventory."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

GUEST_BINARY = "/opt/google/chrome/chrome"
CATALOGER = "maf-google-chrome-package"


def component(sbom: dict[str, Any]) -> dict[str, Any] | None:
    """Derive Chrome's upstream identity from its installed package and inventoried ELF."""
    packages = [a for a in sbom.get("artifacts", []) if a.get("name") == "google-chrome-stable"]
    if not packages:
        if any(f["location"]["path"] == GUEST_BINARY for f in sbom.get("files", [])):
            raise ValueError("Chrome executable has no vendor package inventory")
        return None
    if len(packages) != 1:
        raise ValueError("Ambiguous Google Chrome package inventory")
    package = packages[0]
    version = re.fullmatch(r"(\d+\.\d+\.\d+\.\d+)-\d+", package.get("version", ""))
    metadata = package.get("metadata", {})
    if (
        package.get("type") != "deb"
        or version is None
        or metadata.get("package") != "google-chrome-stable"
        or metadata.get("version") != package["version"]
        or metadata.get("architecture") != "amd64"
        or GUEST_BINARY not in {f.get("path") for f in metadata.get("files", [])}
    ):
        raise ValueError("Incomplete Google Chrome package inventory")
    binaries = [f for f in sbom.get("files", []) if f["location"]["path"] == GUEST_BINARY]
    if len(binaries) != 1:
        raise ValueError("Missing or ambiguous Chrome executable inventory")
    binary = binaries[0]
    digests = [d["value"] for d in binary.get("digests", []) if d["algorithm"] == "sha256"]
    if (
        len(digests) != 1
        or not re.fullmatch(r"[0-9a-f]{64}", digests[0])
        or binary.get("metadata", {}).get("type") != "RegularFile"
        or binary.get("executable", {}).get("format") != "elf"
        or binary.get("executable", {}).get("hasEntrypoint") is not True
    ):
        raise ValueError("Chrome inventory lacks a hashed ELF executable")
    upstream = version[1]
    return {
        "id": hashlib.sha256(f"chrome:{upstream}:{digests[0]}".encode()).hexdigest()[:16],
        "name": "chrome",
        "version": upstream,
        "type": "binary",
        "foundBy": CATALOGER,
        "locations": [binary["location"]],
        "licenses": package.get("licenses", []),
        "language": "",
        "cpes": [
            {"cpe": f"cpe:2.3:a:google:chrome:{upstream}:*:*:*:*:*:*:*", "source": "declared"}
        ],
        "purl": f"pkg:generic/chrome@{upstream}",
    }


def verify(sbom: dict[str, Any]) -> None:
    """Refuse vendor Chrome inventory that cannot participate in upstream CVE matching."""
    expected = component(sbom)
    if expected is not None and expected not in sbom["artifacts"]:
        raise ValueError("Chrome upstream CVE inventory is missing or inconsistent")


def retain(directory: Path) -> bool:
    """Retain raw Syft evidence and add the browser identity to the scanned inventory."""
    path = directory / "sbom.syft.json"
    original = path.read_text(encoding="utf-8")
    sbom = json.loads(original)
    expected = component(sbom)
    if expected is None or expected in sbom["artifacts"]:
        return False
    if any(
        a.get("foundBy") == CATALOGER or a.get("id") == expected["id"] for a in sbom["artifacts"]
    ):
        raise ValueError("Conflicting Chrome upstream CVE inventory")
    (directory / "sbom.raw.syft.json").write_text(original, encoding="utf-8")
    sbom["artifacts"].append(expected)
    path.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    return True
