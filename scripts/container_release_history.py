"""Store the release catalogue as immutable GitHub Release snapshots, outside Git history."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from container_release import (
    REPOSITORY,
    decode,
    empty_catalogue,
    require_digest,
    timestamp,
    validate_catalogue,
    validate_transition,
    write,
)

TAG = "security-history-"
ASSET = "catalogue.json"
MAX_BYTES = 32 * 1024 * 1024
MAX_SEQUENCE = 999_999_999_999


def encode(value: Any) -> bytes:
    """Use stable bytes for snapshot identities and uncertain-write reconciliation."""
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def sha256(raw: bytes) -> str:
    """Identify a complete snapshot including its parent and operation identity."""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def sequence(release: dict[str, Any]) -> int | None:
    """Ignore software releases but reject malformed records in the reserved namespace."""
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith(TAG):
        return None
    match = re.fullmatch(TAG + r"([0-9]{12})", tag)
    if match is None or int(match[1]) == 0:
        raise ValueError("Malformed security-history release tag")
    return int(match[1])


class GitHub:
    """Use the fixed GitHub repository and CLI credential handling for release assets."""

    def request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: bytes | Path | None = None,
        binary: bool = False,
        upload: bool = False,
    ) -> bytes:
        """Leave uncertain mutations to reconciliation rather than blindly retrying them."""
        command = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            endpoint,
            "--method",
            method,
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "-H",
            "Cache-Control: no-cache",
            "-H",
            "Accept: application/octet-stream" if binary else "Accept: application/vnd.github+json",
        ]
        if payload is not None:
            command.extend(
                [
                    "--input",
                    str(payload) if isinstance(payload, Path) else "-",
                    "-H",
                    "Content-Type: application/octet-stream"
                    if upload
                    else "Content-Type: application/json",
                ]
            )
        result = subprocess.run(
            command,
            input=payload if isinstance(payload, bytes) else None,
            capture_output=True,
            check=True,
            timeout=600,
        )
        if len(result.stdout) > MAX_BYTES:
            raise ValueError("GitHub response exceeds the history size limit")
        return result.stdout

    def pages(self, endpoint: str) -> list[dict[str, Any]]:
        """Read every API page; incomplete pagination cannot establish the latest record."""
        values: list[dict[str, Any]] = []
        for page in range(1, 10_001):
            batch = decode(self.request(f"{endpoint}?per_page=100&page={page}"))
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise ValueError("Unexpected GitHub list response")
            values.extend(batch)
            if len(batch) < 100:
                return values
        raise ValueError("History pagination exceeded its bound")

    def releases(self) -> list[dict[str, Any]]:
        """Include drafts when the caller has write access, without relying on latest-release."""
        return self.pages(f"repos/{REPOSITORY}/releases")

    def assets(self, release_id: int) -> list[dict[str, Any]]:
        """Read complete asset metadata separately from the abbreviated release listing."""
        return self.pages(f"repos/{REPOSITORY}/releases/{release_id}/assets")

    def download(self, asset_id: int) -> bytes:
        """Retrieve original bytes through the fixed repository's asset API."""
        return self.request(f"repos/{REPOSITORY}/releases/assets/{asset_id}", binary=True)

    def download_file(self, asset_id: int, destination: Path) -> None:
        """Stream large retained reports rather than buffering entire archives in memory."""
        positive_id(asset_id)
        with destination.open("xb") as output:
            subprocess.run(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    f"repos/{REPOSITORY}/releases/assets/{asset_id}",
                    "-H",
                    "Accept: application/octet-stream",
                    "-H",
                    "Cache-Control: no-cache",
                ],
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
                timeout=600,
            )

    def tag_commit(self, tag: str) -> str | None:
        """Resolve an existing tag instead of trusting the nominal target_commitish field."""
        if not re.fullmatch(r"[a-z][a-z0-9.-]{0,127}", tag):
            raise ValueError("Invalid release tag")
        try:
            value = decode(self.request(f"repos/{REPOSITORY}/git/ref/tags/{tag}"))["object"]
        except subprocess.CalledProcessError as error:
            if b"HTTP 404" in (error.stderr or b""):
                return None
            raise
        for _ in range(8):
            revision = value.get("sha")
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("Invalid history tag target")
            if value.get("type") == "commit":
                return revision
            if value.get("type") != "tag":
                raise ValueError("History tag does not identify a commit")
            value = decode(self.request(f"repos/{REPOSITORY}/git/tags/{revision}"))["object"]
        raise ValueError("History tag nesting exceeds its bound")

    def create(self, tag: str, source: str) -> dict[str, Any]:
        """Prepare a history entry without publishing incomplete evidence."""
        existing = self.tag_commit(tag)
        if existing is not None and existing != source:
            raise ValueError("Existing history tag identifies another source")
        return decode(
            self.request(
                f"repos/{REPOSITORY}/releases",
                method="POST",
                payload=encode(
                    {
                        "tag_name": tag,
                        "target_commitish": source,
                        "name": tag,
                        "body": "Automated container security history. Authoritative state is in catalogue.json.",
                        "draft": True,
                        "prerelease": True,
                        "make_latest": "false",
                    }
                ),
            )
        )

    def upload(self, release_id: int, raw: bytes) -> None:
        """Never clobber an asset; a duplicate upload must be reconciled by its exact bytes."""
        self.request(
            f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release_id}/assets?name={ASSET}",
            method="POST",
            payload=raw,
            upload=True,
        )

    def publish(self, release_id: int) -> None:
        """The immutable publication is the catalogue commit point."""
        self.request(
            f"repos/{REPOSITORY}/releases/{release_id}",
            method="PATCH",
            payload=encode({"draft": False, "prerelease": True, "make_latest": "false"}),
        )


@dataclass(frozen=True)
class Snapshot:
    """A validated immutable state and its precise place in the security history."""

    number: int
    digest: str
    document: dict[str, Any]
    release_id: int

    @property
    def catalogue(self) -> dict[str, Any]:
        """Expose state without using mutable release titles or notes."""
        return self.document["catalogue"]

    @property
    def reference(self) -> dict[str, Any]:
        """Name the predecessor for the next complete snapshot."""
        return {"sequence": self.number, "sha256": self.digest}

    @property
    def lineage(self) -> list[dict[str, Any]]:
        """Bind every committed predecessor without downloading historical scan records."""
        return [*self.document["ancestors"], self.reference]


def positive_id(value: Any) -> int:
    """Validate API identifiers before incorporating them into fixed endpoints."""
    if type(value) is not int or value < 1:
        raise ValueError("Expected a positive GitHub identifier")
    return value


def catalogue_asset(assets: Any) -> dict[str, Any]:
    """Require the complete singleton asset inventory of a history release."""
    if (
        not isinstance(assets, list)
        or len(assets) != 1
        or not isinstance(assets[0], dict)
        or assets[0].get("name") != ASSET
        or assets[0].get("state") != "uploaded"
    ):
        raise ValueError("History has no unique complete catalogue asset")
    return assets[0]


class History:
    """Read and append immutable snapshots; callers serialize all writes in one Actions group."""

    def __init__(self, github: GitHub | None = None) -> None:
        self.github = github or GitHub()

    def records(self) -> dict[int, dict[str, Any]]:
        """Reject conflicting sequence ownership, including unfinished drafts."""
        records: dict[int, dict[str, Any]] = {}
        for release in self.github.releases():
            number = sequence(release)
            if number is None:
                continue
            positive_id(release.get("id"))
            if type(release.get("draft")) is not bool:
                raise ValueError("History release has no draft state")
            if number in records:
                raise ValueError("Conflicting or unstable security-history listing")
            records[number] = release
        return records

    def snapshot(self, number: int, release: dict[str, Any]) -> Snapshot:
        """Verify the original snapshot asset and the envelope before considering its state."""
        assets = self.github.assets(positive_id(release.get("id")))
        asset = catalogue_asset(assets)
        if (
            asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int
            or not 0 < asset["size"] <= MAX_BYTES
        ):
            raise ValueError("Incomplete or oversized history asset")
        raw = self.github.download(positive_id(asset.get("id")))
        hashed = sha256(raw)
        if len(raw) != asset["size"] or hashed != require_digest(asset.get("digest")):
            raise ValueError("History asset bytes differ from GitHub metadata")
        value = decode(raw)
        if (
            not isinstance(value, dict)
            or type(value.get("schemaVersion")) is not int
            or value["schemaVersion"] != 1
            or type(value.get("sequence")) is not int
            or value["sequence"] != number
            or not isinstance(value.get("sourceCommit"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["sourceCommit"])
            or not isinstance(value.get("operationId"), str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_.-]{0,199}", value["operationId"])
            or "previous" not in value
            or not isinstance(value.get("ancestors"), list)
        ):
            raise ValueError("Invalid security-history envelope")
        timestamp(value.get("createdAt"))
        if release.get("target_commitish") != value["sourceCommit"]:
            raise ValueError("History release source differs from the snapshot")
        last = 0
        for ancestor in value["ancestors"]:
            if (
                not isinstance(ancestor, dict)
                or set(ancestor) != {"sequence", "sha256"}
                or type(ancestor["sequence"]) is not int
                or not last < ancestor["sequence"] < number
            ):
                raise ValueError("Invalid history ancestry")
            require_digest(ancestor["sha256"])
            last = ancestor["sequence"]
        if value["previous"] != (value["ancestors"][-1] if value["ancestors"] else None):
            raise ValueError("History predecessor differs from its ancestry")
        validate_catalogue(value.get("catalogue"))
        return Snapshot(number, hashed, value, release["id"])

    def head(self) -> Snapshot | None:
        """Check retained ancestry and current bytes without downloading every old snapshot."""
        records = self.records()
        committed = {n: r for n, r in records.items() if not r["draft"]}
        references = []
        for number, release in sorted(committed.items()):
            if release.get("immutable") is not True:
                raise ValueError("Published security history must be immutable")
            asset = catalogue_asset(release.get("assets"))
            references.append({"sequence": number, "sha256": require_digest(asset.get("digest"))})
        current = self.snapshot(max(committed), committed[max(committed)]) if committed else None
        if current and current.lineage != references:
            raise ValueError("Security history has a fork, changed asset or missing predecessor")
        if (
            current
            and self.github.tag_commit(f"{TAG}{current.number:012d}")
            != current.document["sourceCommit"]
        ):
            raise ValueError("Committed history tag identifies another source")
        checked = {n: r["id"] for n, r in self.records().items() if not r["draft"]}
        if checked != {n: r["id"] for n, r in committed.items()}:
            raise ValueError("Security history changed during retrieval")
        return current

    def append(
        self,
        transform: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        operation: str,
        source: str,
        at: str,
    ) -> Snapshot | None:
        """Commit one transition, reconciling matching drafts and rejecting stale ones."""
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_.-]{0,199}", operation):
            raise ValueError("Invalid history operation identity")
        if not re.fullmatch(r"[0-9a-f]{40}", source):
            raise ValueError("Expected the full workflow source commit")
        timestamp(at)
        before = self.head()
        state = before.catalogue if before else empty_catalogue()
        # Transforms must not mutate the committed snapshot used for comparison.
        after = transform(decode(encode(state)))
        validate_transition(state, after)
        if after == state:
            return before
        parent = before.reference if before else None
        if before and timestamp(at) < timestamp(before.document["createdAt"]):
            raise ValueError("History transaction predates its parent")
        records = self.records()
        matching: list[Snapshot] = []
        for number, release in records.items():
            if not release["draft"]:
                continue
            assets = self.github.assets(release["id"])
            if not any(
                asset.get("name") == ASSET and asset.get("state") == "uploaded" for asset in assets
            ):
                continue
            draft = self.snapshot(number, release)
            if draft.document["operationId"] == operation:
                matching.append(draft)
        if len(matching) > 1:
            raise ValueError("Multiple drafts claim the same history operation")
        if matching:
            planned = matching[0]
            if (
                planned.document["previous"] != parent
                or planned.catalogue != after
                or planned.document["sourceCommit"] != source
            ):
                raise ValueError("Retry differs from the retained draft or its predecessor")
        else:
            number = max(records, default=0) + 1
            if number > MAX_SEQUENCE:
                raise ValueError("History sequence exhausted")
            document = {
                "schemaVersion": 1,
                "sequence": number,
                "previous": parent,
                "ancestors": before.lineage if before else [],
                "operationId": operation,
                "sourceCommit": source,
                "createdAt": at,
                "catalogue": after,
            }
            raw = encode(document)
            if len(raw) > MAX_BYTES:
                raise ValueError("History snapshot exceeds its size limit")
            release = self.github.create(f"{TAG}{number:012d}", source)
            if sequence(release) != number or release.get("draft") is not True:
                raise ValueError("GitHub did not create the expected history draft")
            self.github.upload(positive_id(release.get("id")), raw)
            planned = self.snapshot(number, release)
            if planned.digest != sha256(raw):
                raise ValueError("Uploaded draft differs from the planned transaction")
        current = self.head()
        if (current.reference if current else None) != parent:
            raise ValueError("History head changed before publication")
        self.github.publish(planned.release_id)
        committed = self.head()
        if committed is None or committed.reference != planned.reference:
            raise ValueError("History publication could not be confirmed")
        return committed


def main() -> None:
    """Export validated committed history; mutations are called only by trusted workflow jobs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    head = History().head()
    write(args.output, head.document if head else {"catalogue": empty_catalogue()})


if __name__ == "__main__":
    main()
