"""A conversation's compacted view, persisted in two parts instead of rebuilt on every pass (#2995).

The **earlier-turns** part holds the compacted form of every completed turn and changes only when
a turn ends or compaction edits it. The **current-turn** part holds the live turn and is saved
after every pass. Each part is the per-turn saved view's layout (:mod:`._saved_view`) cut to the
range of stored messages it covers, bound to the compaction configuration that produced it, and
able to say why it no longer fits a stored history.

A host supplies storage through :class:`CompactedViewStore`. Every write names the version it
replaces, so two writers -- two replicas, or an advisor and a sub-agent -- cannot overwrite a
newer view: the loser gets :class:`ViewWriteConflict` and rebuilds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from agent_framework import Message

from ._view_digest import canonical_json, content_digest

#: Format of a serialized :class:`PersistedViewPart`; a part of another version is not read.
VIEW_PART_VERSION: Final[int] = 1
_USER_ROLE: Final[str] = "user"


class ViewPart(StrEnum):
    """Which part of a conversation's compacted view."""

    EARLIER = "earlier"
    CURRENT = "current"


class MisfitReason(StrEnum):
    """Why a persisted part no longer fits a stored history."""

    VERSION = "version"
    CONFIG = "config"
    RANGE = "range"
    DIGEST = "digest"


@dataclass(frozen=True)
class ViewKey:
    """Where one part of one agent's view of one conversation is stored."""

    conversation: str
    agent: str
    part: ViewPart


@dataclass(frozen=True)
class StoredView:
    """A part as the store holds it, with the version a write must name to replace it."""

    data: dict[str, Any]
    etag: str


class ViewWriteConflict(Exception):
    """The stored part is not the version the writer read: someone else wrote a newer one."""


class CompactedViewStore(Protocol):
    """Storage for persisted view parts, supplied by the host."""

    async def read(self, key: ViewKey) -> StoredView | None:
        """Return the stored part, or None when there is none."""
        ...

    async def write(self, key: ViewKey, data: dict[str, Any], *, etag: str | None) -> str:
        """Store ``data`` if the stored version is ``etag`` (None: nothing stored yet).

        Returns the new version. Raises :class:`ViewWriteConflict` otherwise.
        """
        ...

    async def delete(self, key: ViewKey) -> None:
        """Remove the part; removing a missing one is not an error."""
        ...


class InMemoryViewStore:
    """A process-local :class:`CompactedViewStore`, for tests and single-process hosts."""

    def __init__(self) -> None:
        self._parts: dict[ViewKey, StoredView] = {}
        self._writes = 0

    async def read(self, key: ViewKey) -> StoredView | None:
        stored = self._parts.get(key)
        return (
            None if stored is None else StoredView(json.loads(json.dumps(stored.data)), stored.etag)
        )

    async def write(self, key: ViewKey, data: dict[str, Any], *, etag: str | None) -> str:
        stored = self._parts.get(key)
        if (stored.etag if stored is not None else None) != etag:
            raise ViewWriteConflict(f"{key.part} view of {key.agent!r} changed since it was read")
        self._writes += 1
        new = str(self._writes)
        self._parts[key] = StoredView(json.loads(json.dumps(data)), new)
        return new

    async def delete(self, key: ViewKey) -> None:
        self._parts.pop(key, None)


@dataclass(frozen=True)
class PersistedViewPart:
    """One part of the view: a saved-view layout over stored messages ``start`` to ``end``.

    ``layout`` is the saved view's canonical JSON with source positions counted from ``start``;
    ``config`` fingerprints the compaction configuration and prompts that produced it.
    """

    part: ViewPart
    start: int
    end: int
    layout: str
    config: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VIEW_PART_VERSION,
            "part": str(self.part),
            "start": self.start,
            "end": self.end,
            "layout": self.layout,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersistedViewPart | None:
        """Read a serialized part; None for another version or a malformed one."""
        try:
            if data["version"] != VIEW_PART_VERSION:
                return None
            part = cls(
                ViewPart(data["part"]),
                int(data["start"]),
                int(data["end"]),
                str(data["layout"]),
                str(data["config"]),
            )
            json.loads(part.layout)
        except (KeyError, TypeError, ValueError):
            return None
        return part if 0 <= part.start <= part.end else None

    def misfit(self, messages: list[Message], config: str) -> MisfitReason | None:
        """Why this part does not fit ``messages`` under ``config``, or None when it does.

        Each stored message the part covers must still have the contents the layout recorded.
        """
        if self.config != config:
            return MisfitReason.CONFIG
        if self.end > len(messages):
            return MisfitReason.RANGE
        try:
            sources = json.loads(self.layout)["sources"]
        except (KeyError, TypeError, ValueError):
            return MisfitReason.VERSION
        if len(sources) != self.end - self.start:
            return MisfitReason.RANGE
        for offset, source in enumerate(sources):
            if content_digest(messages[self.start + offset].contents) != source[0]:
                return MisfitReason.DIGEST
        return None


def turn_boundary(messages: list[Message]) -> int:
    """Index of the message the current turn starts at: the last user message, or 0."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == _USER_ROLE:
            return index
    return 0


def split_saved_layout(layout: str, boundary: int) -> tuple[str, str] | None:
    """Cut a saved-view layout at stored message ``boundary`` into earlier and current layouts.

    Sources before the boundary go to the earlier part, the rest to the current part with their
    positions counted from the boundary. An inserted message (a record, summary or note) goes
    with the part of the source entry before it, or with the earlier part when none precedes it.
    Returns None for a malformed layout or a boundary outside it.
    """
    try:
        data = json.loads(layout)
        sources, order, required = data["sources"], data["order"], int(data["required"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= boundary <= len(sources):
        return None
    earlier_order: list[dict[str, Any]] = []
    current_order: list[dict[str, Any]] = []
    in_current = False
    try:
        for entry in order:
            if "source" in entry:
                in_current = int(entry["source"]) >= boundary
                if in_current:
                    current_order.append({**entry, "source": int(entry["source"]) - boundary})
                    continue
            (current_order if in_current else earlier_order).append(entry)
    except (TypeError, ValueError):
        return None
    earlier = {
        "sources": sources[:boundary],
        "order": earlier_order,
        "required": min(required, boundary),
    }
    current = {
        "sources": sources[boundary:],
        "order": current_order,
        "required": max(required - boundary, 0),
    }
    return canonical_json(earlier), canonical_json(current)


__all__ = [
    "VIEW_PART_VERSION",
    "CompactedViewStore",
    "InMemoryViewStore",
    "MisfitReason",
    "PersistedViewPart",
    "StoredView",
    "ViewKey",
    "ViewPart",
    "ViewWriteConflict",
    "split_saved_layout",
    "turn_boundary",
]
