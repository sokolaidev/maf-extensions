"""Host-owned, immutable database updates for an offline scan guest."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

DATABASES = ("main", "daily", "bytecode")
MAX_UPDATE_BYTES = 95 * 1024 * 1024
UPDATE_CHUNK_BYTES = 16 * 1024 * 1024
MAX_PATCHES = 90
NAME = re.compile(r"(main|daily|bytecode)(\.cvd|-[1-9][0-9]{0,9}\.cdiff)")


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    """One database in the image seed or selected target."""

    name: str
    version: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UpdateFile:
    """An upstream CVD replacement or signed CDIFF, never an unsigned script."""

    name: str
    data: bytes


@dataclass(frozen=True, slots=True)
class DatabaseUpdate:
    """Bind an update to all three seed databases and exact target versions.

    Missing databases keep their seed version. A database uses either one CVD replacement
    or a consecutive CDIFF chain; the guest authenticates upstream signatures before use.
    """

    seed: tuple[DatabaseIdentity, ...]
    targets: tuple[tuple[str, int], ...]
    files: tuple[UpdateFile, ...] = ()

    def manifest(self) -> dict[str, object]:
        """Validate the bounded transfer and produce its wire manifest."""
        if type(self.seed) is not tuple or type(self.targets) is not tuple:
            raise ValueError("seed and targets must be tuples")
        seeds = {item.name: item for item in self.seed}
        targets = dict(self.targets)
        if len(self.seed) != 3 or set(seeds) != set(DATABASES):
            raise ValueError("exactly main, daily and bytecode seeds are required")
        if len(self.targets) != 3 or set(targets) != set(DATABASES):
            raise ValueError("exactly main, daily and bytecode targets are required")
        for name, seed in seeds.items():
            if (
                type(seed.version) is not int
                or seed.version < 1
                or re.fullmatch(r"[0-9a-f]{64}", seed.sha256) is None
                or type(targets[name]) is not int
                or targets[name] < seed.version
            ):
                raise ValueError("invalid database identity or rollback")
        if type(self.files) is not tuple or len(self.files) > MAX_PATCHES + 3:
            raise ValueError("too many update files")
        names: set[str] = set()
        total = 0
        records: list[dict[str, object]] = []
        for item in self.files:
            if NAME.fullmatch(item.name) is None or item.name in names:
                raise ValueError("invalid or duplicate update name")
            if type(item.data) is not bytes or not item.data:
                raise ValueError("update content must be nonempty bytes")
            names.add(item.name)
            total += len(item.data)
            if total > MAX_UPDATE_BYTES:
                raise ValueError("max_update_bytes exceeded")
            records.append(
                {
                    "name": item.name,
                    "size": len(item.data),
                    "sha256": hashlib.sha256(item.data).hexdigest(),
                }
            )
        patches = 0
        for name, seed in seeds.items():
            selected = {value for value in names if value.startswith(name + "-")}
            if name + ".cvd" in names:
                if selected:
                    raise ValueError("replacement and patches cannot be combined")
            else:
                count = targets[name] - seed.version
                patches += count
                if patches > MAX_PATCHES:
                    raise ValueError("max_patches exceeded; supply a full replacement")
                expected = {
                    f"{name}-{version}.cdiff"
                    for version in range(seed.version + 1, targets[name] + 1)
                }
                if selected != expected:
                    raise ValueError("missing or unexpected CDIFF in update chain")
        return {
            "schema_version": 1,
            "seed": {
                name: {"version": item.version, "sha256": item.sha256}
                for name, item in seeds.items()
            },
            "targets": targets,
            "files": records,
        }


DatabaseUpdateProvider = Callable[[tuple[DatabaseIdentity, ...]], Awaitable[DatabaseUpdate]]


def directory_update_provider(directory: Path) -> DatabaseUpdateProvider:
    """Read a host-owned mirror snapshot containing CVDs and optional retained CDIFFs.

    Publish the directory atomically and keep it immutable during a call. Acquisition and
    refresh belong to the host; this provider never contacts upstream or parses uploads.
    """
    directory = directory.absolute()

    def read(seed: tuple[DatabaseIdentity, ...]) -> DatabaseUpdate:
        snapshot = directory.resolve()
        targets: list[tuple[str, int]] = []
        payload: list[UpdateFile] = []
        total = 0
        patch_count = 0
        for item in seed:
            if item.name not in DATABASES:
                raise ValueError("unknown seed database")
            replacement = snapshot / f"{item.name}.cvd"
            with replacement.open("rb") as stream:
                header = stream.read(512).decode("ascii").split(":")
            if len(header) != 9 or header[0] != "ClamAV-VDB":
                raise ValueError("invalid mirror database header")
            target = int(header[2])
            if target < item.version:
                raise ValueError("mirror is older than the image seed")
            targets.append((item.name, target))
            if target == item.version:
                continue
            count = target - item.version
            paths: list[Path] = []
            if count + patch_count <= MAX_PATCHES:
                paths = [
                    snapshot / f"{item.name}-{version}.cdiff"
                    for version in range(item.version + 1, target + 1)
                ]
            if not paths or not all(path.is_file() and path.stat().st_size for path in paths):
                paths = [replacement]
            else:
                patch_count += count
            for path in paths:
                with path.open("rb") as stream:
                    data = stream.read(MAX_UPDATE_BYTES - total + 1)
                total += len(data)
                if total > MAX_UPDATE_BYTES:
                    raise ValueError("max_update_bytes exceeded")
                payload.append(UpdateFile(path.name, data))
        update = DatabaseUpdate(seed, tuple(targets), tuple(payload))
        update.manifest()
        return update

    async def provide(seed: tuple[DatabaseIdentity, ...]) -> DatabaseUpdate:
        return await asyncio.to_thread(read, seed)

    return provide
