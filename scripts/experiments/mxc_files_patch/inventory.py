"""Plan bounded upload replacement without mutating committed workspace contents."""

from __future__ import annotations

import errno
from dataclasses import dataclass, replace

from scripts.experiments.mxc_files_patch.request import Input, Request, WorkspaceLimits, _names
from scripts.experiments.mxc_session_patch.host_store import Refused


@dataclass(frozen=True)
class Entry:
    """Immutable bytes and lifetime for one regular file in a captured inventory."""

    name: str
    data: bytes
    lifecycle: str

    def __post_init__(self) -> None:
        Input(self.name, self.data, self.lifecycle)


def validate(entries: tuple[Entry, ...], limits: WorkspaceLimits) -> None:
    """Refuse an invalid or over-budget inventory before preparing any replacements."""
    if type(entries) is not tuple or any(not isinstance(entry, Entry) for entry in entries):
        raise Refused("invalid workspace inventory")
    if len(entries) > limits.files or sum(len(entry.data) for entry in entries) > limits.bytes:
        raise Refused("workspace inventory exceeds allowance")
    _names(tuple(entry.name for entry in entries))


def stage(entries: tuple[Entry, ...], request: Request) -> tuple[Entry, ...]:
    """Return a new inventory only if every upload and its combined charge is admissible."""
    validate(entries, request.workspace)
    updated = {entry.name: entry for entry in entries}
    aliases = {entry.name.casefold(): entry.name for entry in entries}
    for item in request.inputs:
        existing = aliases.get(item.name.casefold())
        if existing is not None and (existing != item.name or not item.replace):
            raise Refused("upload requires an exact name and explicit replacement authority")
        updated[item.name] = Entry(item.name, item.data, item.lifecycle)
    result = tuple(updated[key] for key in sorted(updated))
    validate(result, request.workspace)
    return result


def _target(entries: tuple[Entry, ...], path: str, limits: WorkspaceLimits) -> Entry:
    validate(entries, limits)
    for entry in entries:
        if entry.name == path:
            return entry
    raise FileNotFoundError(errno.ENOENT, "workspace file does not exist", path)


def _growth(entries: tuple[Entry, ...], entry: Entry, length: int, limits: WorkspaceLimits) -> None:
    if sum(len(item.data) for item in entries) - len(entry.data) + length > limits.bytes:
        raise OSError(errno.ENOSPC, "workspace byte allowance exceeded", entry.name)


def write(
    entries: tuple[Entry, ...],
    path: str,
    data: bytes,
    offset: int,
    limits: WorkspaceLimits,
    *,
    append: bool = False,
) -> tuple[Entry, ...]:
    """Model one whole write; refusal preserves the input inventory and byte capacity."""
    entry = _target(entries, path, limits)
    if type(data) is not bytes or type(offset) is not int or offset < 0 or type(append) is not bool:
        raise OSError(errno.EINVAL, "invalid workspace write", path)
    if not data:
        return entries
    start = len(entry.data) if append else offset
    end = start + len(data)
    length = max(len(entry.data), end)
    _growth(entries, entry, length, limits)
    content = bytearray(entry.data)
    content.extend(b"\0" * (length - len(content)))
    content[start:end] = data
    updated = replace(entry, data=bytes(content))
    return tuple(updated if item.name == path else item for item in entries)


def truncate(
    entries: tuple[Entry, ...],
    path: str,
    length: int,
    limits: WorkspaceLimits,
) -> tuple[Entry, ...]:
    """Model a resize, checking growth before allocation and retaining the file lifetime."""
    entry = _target(entries, path, limits)
    if type(length) is not int or length < 0:
        raise OSError(errno.EINVAL, "invalid workspace length", path)
    _growth(entries, entry, length, limits)
    data = entry.data[:length] + b"\0" * max(0, length - len(entry.data))
    updated = replace(entry, data=data)
    return tuple(updated if item.name == path else item for item in entries)


def complete(
    entries: tuple[Entry, ...], request: Request
) -> tuple[tuple[Entry, ...], tuple[Entry, ...]]:
    """Validate all artifacts from one immutable inventory before selecting retained state."""
    validate(entries, request.workspace)
    indexed = {entry.name: entry for entry in entries}
    artifacts = []
    total = 0
    for path in sorted(request.artifacts):
        entry = indexed.get(path)
        if entry is None:
            raise Refused("requested artifact is missing")
        total += len(entry.data)
        if len(entry.data) > request.limits.file_bytes or total > request.limits.artifact_bytes:
            raise Refused("requested artifacts exceed allowance")
        artifacts.append(entry)
    retained = tuple(entry for entry in entries if entry.lifecycle == "session")
    return retained, tuple(artifacts)
