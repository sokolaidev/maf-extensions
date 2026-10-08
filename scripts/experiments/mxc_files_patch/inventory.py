"""Plan bounded upload replacement without mutating committed workspace contents."""

from __future__ import annotations

from dataclasses import dataclass

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
