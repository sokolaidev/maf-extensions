"""Retain official APK security feeds separately from unmodified scanner findings."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from container_release import now, read, timestamp, write

SOURCES = {
    "wolfi": ("wolfi", "https://packages.wolfi.dev/os/security.json"),
    "alpine-main": ("alpine", "https://secdb.alpinelinux.org/v3.24/main.json"),
    "alpine-community": ("alpine", "https://secdb.alpinelinux.org/v3.24/community.json"),
}
MAX_BYTES = 64 * 1024**2
MAX_AGE = timedelta(hours=48)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep security evidence on the reviewed vendor endpoint."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ValueError("Vendor security feed redirects are not allowed")


def download(url: str) -> bytes:
    """Fetch a bounded vendor document with ordinary TLS certificate verification."""
    if url not in {source[1] for source in SOURCES.values()}:
        raise ValueError("Unapproved vendor security feed")
    with urllib.request.build_opener(NoRedirect()).open(url, timeout=60) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Vendor security feed exceeds the size limit")
    return raw


def packages(raw: bytes, name: str) -> dict[str, dict[str, list[str]]]:
    """Accept exact source-package fix declarations, never a package-index version guess."""
    data = json.loads(raw)
    repository = "wolfi" if name == "wolfi" else name.removeprefix("alpine-")
    if (
        data.get("reponame") != repository
        or "x86_64" not in data.get("archs", [])
        or (name != "wolfi" and data.get("distroversion") != "v3.24")
        or not isinstance(data.get("packages"), list)
    ):
        raise ValueError("Vendor security feed identifies another repository")
    result = {}
    for entry in data["packages"]:
        package = entry["pkg"]
        key, fixes = package["name"], package["secfixes"]
        if not isinstance(key, str) or key in result or not isinstance(fixes, dict):
            raise ValueError("Ambiguous vendor package security record")
        if any(
            not isinstance(version, str)
            or not isinstance(ids, list)
            or any(not isinstance(cve, str) for cve in ids)
            for version, ids in fixes.items()
        ):
            raise ValueError("Invalid vendor package security fixes")
        result[key] = fixes
    return result


def acquire(directory: Path) -> dict[str, Any]:
    """Retain every feed and its identity before a plan may use vendor fix metadata."""
    directory.mkdir(parents=True, exist_ok=False)
    receipt: dict[str, Any] = {"fetchedAt": now(), "sources": {}}
    for name, (vendor, url) in SOURCES.items():
        raw = download(url)
        packages(raw, name)
        (directory / f"{name}.json").write_bytes(raw)
        receipt["sources"][name] = {
            "vendor": vendor,
            "url": url,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    write(directory / "receipt.json", receipt)
    return load(directory)


def load(directory: Path) -> dict[str, Any]:
    """Recheck source allowlist, retained bytes and acquisition age on every use."""
    if directory.is_symlink() or (directory / "receipt.json").is_symlink():
        raise ValueError("Vendor evidence symlinks are not supported")
    receipt = read(directory / "receipt.json")
    if not timedelta(0) <= timestamp(now()) - timestamp(receipt["fetchedAt"]) < MAX_AGE:
        raise ValueError("Vendor security evidence expired or is dated in the future")
    if set(receipt["sources"]) != set(SOURCES):
        raise ValueError("Incomplete vendor security evidence")
    records = {}
    for name, (vendor, url) in SOURCES.items():
        path = directory / f"{name}.json"
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            raise ValueError("Invalid vendor security evidence file")
        raw = path.read_bytes()
        if receipt["sources"][name] != {
            "vendor": vendor,
            "url": url,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }:
            raise ValueError("Vendor security evidence identity changed")
        records[name] = packages(raw, name)
    return {"receipt": receipt, "packages": records}


def fixes(evidence: dict[str, Any], component: dict[str, Any], cve: str) -> list[dict[str, str]]:
    """Map a CVE through the SBOM's exact APK vendor, source package and architecture."""
    metadata = component.get("metadata", {})
    name, installed = component["name"], component["version"]
    source = metadata.get("originPackage")
    if (
        component.get("type") != "apk"
        or metadata.get("architecture") != "x86_64"
        or not isinstance(source, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9+_.-]*", source)
    ):
        return []
    result = []
    for feed, (vendor, _) in SOURCES.items():
        if not re.fullmatch(
            rf"pkg:apk/{vendor}/{re.escape(name)}@{re.escape(installed)}(?:[?#].*)?",
            component.get("purl", ""),
        ):
            continue
        for fixed, ids in evidence["packages"][feed].get(source, {}).items():
            # SecDB's zero denotes not-affected, not an installable security update.
            if cve in ids and fixed != "0":
                result.append(
                    {"feed": feed, "sourcePackage": source, "fixedVersion": fixed, "id": cve}
                )
    return result
