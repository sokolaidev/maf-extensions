"""Retain exact evidence assets in GitHub Releases before marking delivery complete."""

from __future__ import annotations

import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from container_release import REPOSITORY, decode, digest, read, require_digest, validate_identity
from container_release_history import GitHub, encode, positive_id

MAX_ASSET_BYTES = 2 * 1024**3 - 1


def image_tag(candidate: dict[str, Any]) -> str:
    """Keep image evidence releases separate from generated Python package releases."""
    validate_identity(candidate)
    return f"image-{candidate['profile']}-v{candidate['version']}"


class Evidence:
    """Upload once, reconcile by hash, and freeze the complete asset set at publication."""

    def __init__(self, github: GitHub | None = None) -> None:
        self.github = github or GitHub()

    def find(self, tag: str) -> dict[str, Any] | None:
        """Require an unambiguous release in the fixed repository."""
        if not re.fullmatch(r"(?:image|image-monitor)-[a-z0-9.-]+", tag):
            raise ValueError("Invalid evidence release tag")
        found = [release for release in self.github.releases() if release.get("tag_name") == tag]
        if len(found) > 1:
            raise ValueError("Ambiguous evidence release")
        return found[0] if found else None

    def ensure(self, tag: str, source: str) -> dict[str, Any]:
        """Create only a draft so every evidence file can be attached before immutability."""
        if not re.fullmatch(r"[0-9a-f]{40}", source):
            raise ValueError("Expected a full evidence source commit")
        release = self.find(tag)
        existing = self.github.tag_commit(tag)
        if existing is not None and existing != source:
            raise ValueError("Evidence tag identifies another source")
        if release is None:
            release = decode(
                self.github.request(
                    f"repos/{REPOSITORY}/releases",
                    method="POST",
                    payload=encode(
                        {
                            "tag_name": tag,
                            "target_commitish": source,
                            "name": tag,
                            "body": "Container security evidence. Verify the signed image identity and indexed files before use.",
                            "draft": True,
                            "prerelease": True,
                            "make_latest": "false",
                        }
                    ),
                )
            )
        if release.get("target_commitish") != source or release.get("tag_name") != tag:
            raise ValueError("Evidence release source differs from the selected release")
        positive_id(release.get("id"))
        if release.get("draft") is not True and release.get("immutable") is not True:
            raise ValueError("Published evidence is not immutable")
        return release

    def retain(self, release: dict[str, Any], directory: Path, files: dict[str, str]) -> None:
        """Never replace an uploaded file, including one left by an uncertain request."""
        if directory.is_symlink():
            raise ValueError("Evidence directory must not be a symlink")
        rid = positive_id(release.get("id"))
        inventory = self.github.assets(rid)
        by_name = {asset.get("name"): asset for asset in inventory}
        if len(by_name) != len(inventory):
            raise ValueError("Duplicate evidence asset names")
        for name, expected in files.items():
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
                raise ValueError("Invalid evidence asset name")
            require_digest(expected)
            path = directory / name
            if (
                path.is_symlink()
                or not path.is_file()
                or not 0 < path.stat().st_size <= MAX_ASSET_BYTES
                or digest(path) != expected
            ):
                raise ValueError("Local evidence differs from its index")
            asset = by_name.get(name)
            if asset is None:
                if release.get("draft") is not True:
                    raise ValueError("Published immutable evidence is missing a required asset")
                self.github.request(
                    f"https://uploads.github.com/repos/{REPOSITORY}/releases/{rid}/assets?name={urllib.parse.quote(name, safe='')}",
                    method="POST",
                    payload=path,
                    upload=True,
                )
                matches = [a for a in self.github.assets(rid) if a.get("name") == name]
                if len(matches) != 1:
                    raise ValueError("Evidence upload could not be confirmed")
                asset = matches[0]
            with tempfile.TemporaryDirectory(prefix="maf-evidence-readback-") as temporary:
                self.download(asset, Path(temporary) / "asset", expected)

    def fetch(self, tag: str, directory: Path) -> dict[str, Any]:
        """Restore retained evidence for delivery-only retries without rebuilding an image."""
        release = self.find(tag)
        if release is None:
            raise ValueError("Retained release evidence is unavailable")
        directory.mkdir(parents=True, exist_ok=True)
        for asset in self.github.assets(positive_id(release.get("id"))):
            name = asset.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
                raise ValueError("Unsafe retained evidence filename")
            path = directory / name
            if directory.is_symlink() or path.exists():
                raise ValueError("Evidence restore requires fresh file destinations")
            self.download(asset, path, require_digest(asset.get("digest")))
        return release

    def download(self, asset: dict[str, Any], path: Path, expected: str) -> None:
        """Check remote metadata and the downloaded bytes before treating evidence as durable."""
        if (
            asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int
            or not 0 < asset["size"] <= MAX_ASSET_BYTES
            or asset.get("digest") != expected
        ):
            raise ValueError("Invalid or oversized retained evidence asset")
        self.github.download_file(positive_id(asset.get("id")), path)
        if path.stat().st_size != asset["size"] or digest(path) != expected:
            raise ValueError("Durable evidence differs from its index")

    def publish(self, release: dict[str, Any], source: str) -> dict[str, Any]:
        """Confirm immutability and source after publishing the fully assembled evidence."""
        if release.get("draft") is True:
            self.github.publish(positive_id(release.get("id")))
        current = self.find(release["tag_name"])
        if (
            current is None
            or current.get("immutable") is not True
            or current.get("draft") is not False
        ):
            raise ValueError("Immutable evidence publication could not be confirmed")
        if self.github.tag_commit(current["tag_name"]) != source:
            raise ValueError("Published evidence tag identifies another source")
        return current


def indexed_files(directory: Path) -> dict[str, str]:
    """Include the signed index while keeping the completion bundle outside its own hash input."""
    files = read(directory / "evidence-index.json")
    return files | {"evidence-index.json": digest(directory / "evidence-index.json")}
