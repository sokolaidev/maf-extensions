"""Bounded checkpoint inventories and streaming bytes for the coordinator experiment."""

from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Callable
from pathlib import Path

from .host_store import CHUNK, MAX_CHECKPOINT, MAX_FILES, Refused, _relative
from .private_root import check_file
from .shared_store import PATH_BYTES

MAX_DEPTH = 16
MAX_ENTRIES = MAX_FILES * MAX_DEPTH


def inventory(value: object, byte_limit: int, file_limit: int) -> list[dict]:
    """Validate portable paths and declared sizes before receiving any file bytes."""
    if not isinstance(value, list) or not 0 < len(value) <= file_limit:
        raise Refused("invalid checkpoint inventory")
    nodes: dict[str, tuple[str, bool]] = {}
    total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise Refused("invalid checkpoint entry")
        name, size, digest = item["path"], item["size"], item["sha256"]
        if type(name) is not str or type(size) is not int or size < 0:
            raise Refused("invalid checkpoint path or size")
        parts = _relative(name).parts
        if len(name.encode()) > PATH_BYTES or len(parts) > MAX_DEPTH:
            raise Refused("checkpoint path exceeds bounds")
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Refused("invalid checkpoint digest")
        total += size
        if total > byte_limit:
            raise Refused("checkpoint exceeds byte limit")
        for index in range(1, len(parts) + 1):
            node = "/".join(parts[:index])
            is_file = index == len(parts)
            previous = nodes.get(node.lower())
            if previous is not None and (previous != (node, False) or is_file):
                raise Refused("checkpoint paths overlap or alias")
            nodes[node.lower()] = node, is_file
    return value


def tree(
    root: Path, byte_limit: int, file_limit: int, *, staging: bool = False
) -> tuple[list[Path], list[Path]]:
    """Inventory a bounded tree without following links; deletion waits for full validation."""
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise Refused("checkpoint root must be a directory without links")
    files: list[Path] = []
    directories: list[Path] = []
    pending = [root]
    total = 0
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            name = path.relative_to(root).as_posix()
            parts = _relative(name).parts
            if len(parts) > MAX_DEPTH + int(staging) or len(name.encode()) > PATH_BYTES + (
                11 if staging else 0
            ):
                raise Refused("checkpoint path exceeds bounds")
            if path.is_symlink() or path.is_junction():
                raise Refused("checkpoint contains links")
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                directories.append(path)
                pending.append(path)
            else:
                check_file(path)
                total += info.st_size
                files.append(path)
            if (
                total > byte_limit
                or len(files) > file_limit
                or len(files) + len(directories) > MAX_ENTRIES + int(staging)
            ):
                raise Refused("checkpoint tree exceeds bounds")
    return files, directories


def describe(root: Path, byte_limit: int, file_limit: int) -> list[dict]:
    """Hash in bounded blocks; sending checks the same inventory again for changes."""
    files, _ = tree(root, byte_limit, file_limit)
    entries = []
    total = 0
    for path in sorted(files):
        digest = hashlib.sha256()
        size = 0
        expected = path.stat().st_size
        total += expected
        if total > byte_limit:
            raise Refused("checkpoint exceeds byte limit")
        with path.open("rb") as stream:
            while block := stream.read(CHUNK):
                size += len(block)
                if size > expected:
                    raise Refused("checkpoint changed during transfer")
                digest.update(block)
        if size != expected:
            raise Refused("checkpoint changed during transfer")
        entries.append(
            dict(path=path.relative_to(root).as_posix(), size=size, sha256=digest.hexdigest())
        )
    return inventory(entries, byte_limit, file_limit)


def send(root: Path, entries: list[dict], write: Callable[[bytes], None]) -> None:
    """Send exactly the advertised bytes and refuse changed files."""
    for item in entries:
        path = root / item["path"]
        check_file(path)
        digest = hashlib.sha256()
        remaining = item["size"]
        with path.open("rb") as stream:
            while remaining:
                block = stream.read(min(CHUNK, remaining))
                if not block:
                    raise Refused("checkpoint changed during transfer")
                remaining -= len(block)
                digest.update(block)
                write(block)
            if stream.read(1) or digest.hexdigest() != item["sha256"]:
                raise Refused("checkpoint changed during transfer")


def receive(root: Path, entries: list[dict], read: Callable[[int], bytes]) -> None:
    """Receive into caller-owned staging, checking each declared file hash."""
    root.mkdir(mode=0o700)
    for item in entries:
        path = root / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        remaining = item["size"]
        with path.open("xb") as stream:
            while remaining:
                block = read(min(CHUNK, remaining))
                if not block or len(block) > remaining:
                    raise Refused("incomplete checkpoint transfer")
                remaining -= len(block)
                digest.update(block)
                stream.write(block)
        if digest.hexdigest() != item["sha256"]:
            raise Refused("checkpoint digest differs")


def clear(root: Path) -> None:
    """Remove only a fully validated bounded staging tree."""
    files, directories = tree(root, MAX_CHECKPOINT, MAX_FILES, staging=True)
    for path in files:
        path.unlink()
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        path.rmdir()
    root.rmdir()
