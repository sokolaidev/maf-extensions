"""A conversation's compacted view, persisted in two parts instead of rebuilt on every pass (#2995).

The **earlier-turns** part holds the compacted form of every completed turn and changes only when
a turn ends or compaction edits it. The **current-turn** part holds the live turn and is saved
after every pass. Each part is the per-turn saved view's layout (:mod:`._saved_view`) cut to the
range of stored messages it covers, bound to the compaction configuration that produced it, and
able to say why it no longer fits a stored history.

A host supplies storage through :class:`CompactedViewStore`. Every write and delete names the
version it expects, so two writers -- two replicas, or an advisor and a sub-agent -- cannot
overwrite or remove a newer view: the loser gets :class:`ViewWriteConflict` and rebuilds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, cast

from agent_framework import Message
from agent_framework._compaction import GROUP_ANNOTATION_KEY, SUMMARY_OF_MESSAGE_IDS_KEY

from ._carryover import Handed
from ._saved_view import _flags, _state  # pyright: ignore[reportPrivateUsage]
from ._view_digest import canonical_json, json_digest

#: Format of a serialized :class:`PersistedViewPart`; a part of another version is not read.
VIEW_PART_VERSION: Final[int] = 1
_USER_ROLE: Final[str] = "user"
#: A layout source: digest, accepted states, accepted flags, accepted link digests.
_SOURCE_FIELDS: Final[int] = 4


class ViewPart(StrEnum):
    """Which part of a conversation's compacted view."""

    EARLIER = "earlier"
    CURRENT = "current"


class MisfitReason(StrEnum):
    """Why a persisted part no longer fits a stored history."""

    CONFIG = "config"
    RANGE = "range"
    DIGEST = "digest"
    IDENTITY = "identity"
    LAYOUT = "layout"


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

    async def write(self, key: ViewKey, data: dict[str, Any], *, etag: str | None) -> str:
        """Store ``data`` if the stored version is ``etag`` (None: nothing stored yet).

        Returns the new version. Raises :class:`ViewWriteConflict` otherwise.
        """
        raise NotImplementedError

    async def delete(self, key: ViewKey, *, etag: str) -> None:
        """Remove the part if the stored version is ``etag``.

        A part already gone is not an error. A newer one -- another writer published after this
        caller read ``etag`` -- raises :class:`ViewWriteConflict` and stays.
        """


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

    async def delete(self, key: ViewKey, *, etag: str) -> None:
        stored = self._parts.get(key)
        if stored is None:
            return
        if stored.etag != etag:
            raise ViewWriteConflict(f"{key.part} view of {key.agent!r} changed since it was read")
        del self._parts[key]


def _parse_layout(layout: str) -> tuple[list[list[Any]], list[dict[str, Any]], int] | None:
    """A layout's sources, order and required count, or None for anything malformed.

    Each source is ``[digest, accepted states, accepted flags, accepted link digests]``; each
    order entry is an ``insert`` mapping or a ``source`` index inside the sources.
    """
    try:
        data = json.loads(layout)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data = cast(dict[str, Any], data)
    sources, order, required = data.get("sources"), data.get("order"), data.get("required")
    if not isinstance(sources, list) or not isinstance(order, list):
        return None
    if not isinstance(required, int) or isinstance(required, bool) or required < 0:
        return None
    checked_sources = cast(list[Any], sources)
    checked_order = cast(list[Any], order)
    for source in checked_sources:
        if not isinstance(source, list):
            return None
        fields = cast(list[Any], source)
        if not (
            len(fields) == _SOURCE_FIELDS
            and isinstance(fields[0], str)
            and all(isinstance(field, list) for field in fields[1:])
        ):
            return None
    for item in checked_order:
        if not isinstance(item, dict):
            return None
        entry = cast(dict[str, Any], item)
        if ("insert" in entry) == ("source" in entry):
            # Exactly one discriminator: the split reads either kind by its key.
            return None
        if "insert" in entry:
            if not isinstance(entry["insert"], dict):
                return None
            continue
        position = entry.get("source")
        if not isinstance(position, int) or isinstance(position, bool):
            return None
        if not 0 <= position < len(checked_sources):
            return None
    return checked_sources, cast(list[dict[str, Any]], checked_order), required


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
        except (KeyError, TypeError, ValueError):
            return None
        if _parse_layout(part.layout) is None:
            return None
        return part if 0 <= part.start <= part.end else None

    def misfit(self, messages: list[Message], config: str) -> MisfitReason | None:
        """Why this part does not fit ``messages`` under ``config``, or None when it does.

        Each stored message the part covers must still be the one the layout recorded: the same
        contents, and a host state, flags and links the layout accepted -- what
        :func:`~._saved_view._match` checks before it resumes a saved view. An identical text
        under another id, role, author or links does not fit.
        """
        if self.config != config:
            return MisfitReason.CONFIG
        parsed = _parse_layout(self.layout)
        if parsed is None:
            return MisfitReason.LAYOUT
        sources, _, _ = parsed
        if (
            not 0 <= self.start <= self.end <= len(messages)
            or len(sources) != self.end - self.start
        ):
            return MisfitReason.RANGE
        covered = messages[self.start : self.end]
        handed = Handed.take(covered)
        for offset, (digest, accepted, flags, known_links) in enumerate(sources):
            if handed.digests[offset] != digest:
                return MisfitReason.DIGEST
            properties = covered[offset].additional_properties
            if (
                _state(handed.states[offset]) not in accepted
                or list(_flags(properties)) not in flags
                or json_digest(handed.links[offset]) not in known_links
            ):
                return MisfitReason.IDENTITY
        return None


def turn_boundary(messages: list[Message]) -> int:
    """Index of the message the current turn starts at: the last user message, or 0.

    For :func:`split_saved_layout`, pass the list the layout was saved from: there each handed
    message is one source, so the index is a source index. A later history can hold stored
    copies of inserted messages, which are not sources, and its indexes can differ.
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == _USER_ROLE:
            return index
    return 0


def join_layouts(earlier: str, current: str) -> str | None:
    """The one layout :func:`split_saved_layout` cut into ``earlier`` and ``current``.

    The current part's source positions are counted from the boundary again. Returns None when
    either part is malformed.
    """
    first, second = _parse_layout(earlier), _parse_layout(current)
    if first is None or second is None:
        return None
    sources, order, required = first
    later_sources, later_order, later_required = second
    shift = len(sources)
    joined_order = [
        *order,
        *(
            {**entry, "source": int(entry["source"]) + shift} if "source" in entry else entry
            for entry in later_order
        ),
    ]
    joined_required = shift + later_required if later_required else required
    return canonical_json(
        {"sources": [*sources, *later_sources], "order": joined_order, "required": joined_required}
    )


def source_turn_boundary(states: list[tuple[Any, ...]]) -> int:
    """:func:`turn_boundary` over a carried view's source states, whose first field is the role."""
    for index in range(len(states) - 1, -1, -1):
        if str(states[index][0]) == _USER_ROLE:
            return index
    return 0


def _covered_positions(insert: dict[str, Any], positions: dict[str, int]) -> list[int] | None:
    """The source positions an inserted summary or note names as what it stands for.

    Empty when it names none. None when it names a message the layout's order does not place
    (``save_view`` leaves a replaced source out of the order): its side cannot be known.
    """
    properties: Any = insert.get("additional_properties")
    if not isinstance(properties, dict):
        return []
    group: Any = cast(dict[str, Any], properties).get(GROUP_ANNOTATION_KEY)
    if not isinstance(group, dict):
        return []
    ids: Any = cast(dict[str, Any], group).get(SUMMARY_OF_MESSAGE_IDS_KEY)
    if not isinstance(ids, list):
        return []
    named = cast(list[Any], ids)
    if not all(isinstance(mid, str) and mid in positions for mid in named):
        return None
    return [positions[mid] for mid in named]


def split_saved_layout(layout: str, boundary: int) -> tuple[str, str] | None:
    """Cut a saved-view layout at source ``boundary`` into earlier and current layouts.

    ``boundary`` is a source index -- :func:`turn_boundary` over the list the layout was saved
    from. Sources before it go to the earlier part, the rest to the current part with their
    positions counted from it. An inserted message that names the sources it stands for (a user
    summary, which is inserted before them, or a note) goes with the part holding them. One
    that names none (a written record, inserted after its sources) goes with the part of the
    source entry before it, or the earlier part when none precedes it.

    Returns None for a malformed layout, a boundary outside it, an insert that stands for
    sources on both sides of the boundary, which neither part can hold alone, or one that names
    a source the order does not place, whose side cannot be known.
    """
    parsed = _parse_layout(layout)
    if parsed is None:
        return None
    sources, order, required = parsed
    if not 0 <= boundary <= len(sources):
        return None
    positions = {
        str(entry["id"]): int(entry["source"])
        for entry in order
        if "source" in entry and isinstance(entry.get("id"), str)
    }
    earlier_order: list[dict[str, Any]] = []
    current_order: list[dict[str, Any]] = []
    in_current = False
    for entry in order:
        if "source" in entry:
            in_current = int(entry["source"]) >= boundary
            if in_current:
                current_order.append({**entry, "source": int(entry["source"]) - boundary})
            else:
                earlier_order.append(entry)
            continue
        covered = _covered_positions(entry["insert"], positions)
        if covered is None:
            return None
        if covered:
            sides = {position >= boundary for position in covered}
            if len(sides) > 1:
                return None
            (current_order if sides.pop() else earlier_order).append(entry)
        else:
            (current_order if in_current else earlier_order).append(entry)
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
    "join_layouts",
    "source_turn_boundary",
    "split_saved_layout",
    "turn_boundary",
]
