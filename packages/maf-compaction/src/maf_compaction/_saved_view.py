"""The portable layout of a completed per-call pass."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from copy import copy
from typing import Any, cast

from agent_framework import Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
)

from ._anchored import EXCLUDE_REASON, _is_marker  # pyright: ignore[reportPrivateUsage]
from ._carryover import (
    CarriedView,
    Handed,
    Resumed,
    _host_state,  # pyright: ignore[reportPrivateUsage]
    _links,  # pyright: ignore[reportPrivateUsage]
    _same_contents,  # pyright: ignore[reportPrivateUsage]
    apply_view,
)
from ._preserve import PRESERVE_REASON_KEY, PRESERVED_KEY
from ._shed import durable_message_id
from ._toolsummary import (
    CONSOLIDATE_EXCLUDE_REASON,
    PRESERVE_REASON,
    PRESERVE_REASON_UNCOVERED,
    PRESERVE_REASON_UNRECORDED,
    _is_written_record,  # pyright: ignore[reportPrivateUsage]
)
from ._usersummary import EXCLUDE_REASON as USER_EXCLUDE_REASON
from ._usersummary import (
    FOLD_EXCLUDE_REASON,
    _is_summary,  # pyright: ignore[reportPrivateUsage]
)
from ._usersummary import PRESERVE_REASON as USER_PRESERVE_REASON
from ._view_digest import canonical_json, content_digest, json_digest

_EXCLUSIONS = (EXCLUDED_KEY, EXCLUDE_REASON_KEY)
_VERSION = 3
_LINKS = (SUMMARIZED_BY_SUMMARY_ID_KEY, SUMMARY_OF_GROUP_IDS_KEY, SUMMARY_OF_MESSAGE_IDS_KEY)
_OWN_EXCLUSIONS = {
    EXCLUDE_REASON,
    CONSOLIDATE_EXCLUDE_REASON,
    USER_EXCLUDE_REASON,
    FOLD_EXCLUDE_REASON,
    "tool_summary_anchored",
}
_OWN_HOLDS = (
    PRESERVE_REASON,
    PRESERVE_REASON_UNCOVERED,
    PRESERVE_REASON_UNRECORDED,
    USER_PRESERVE_REASON,
    "user_media",
)


def _layout_digest(decisions: dict[str, Any], layout: str) -> str:
    checksum = hashlib.sha256(canonical_json(decisions).encode())
    checksum.update(b"\0")
    checksum.update(layout.encode())
    return checksum.hexdigest()


def _state(state: tuple[Any, ...]) -> str:
    role, author, durable_id, properties = state
    props = dict(properties)
    # A fresh pass derives its own holds. Host holds and exclusions remain part of identity.
    if props.get(PRESERVE_REASON_KEY) in _OWN_HOLDS:
        props.pop(PRESERVED_KEY, None)
        props.pop(PRESERVE_REASON_KEY)
    if props.get(EXCLUDE_REASON_KEY) in _OWN_EXCLUSIONS:
        for key in _EXCLUSIONS:
            props.pop(key, None)
    return json_digest([role, author, durable_id, props])


def _overlay(message: Message) -> dict[str, Any]:
    properties = message.additional_properties
    group = properties.get(GROUP_ANNOTATION_KEY, {})
    return {
        **{key: properties[key] for key in _EXCLUSIONS if key in properties},
        GROUP_ANNOTATION_KEY: {key: group[key] for key in _LINKS if key in group},
    }


def _standing(message: Message, mid: str | None, overlay: dict[str, Any]) -> Message:
    if message.message_id == mid and _overlay(message) == overlay:
        return message
    kept = copy(message)
    kept.message_id = mid
    props = kept.additional_properties = dict(message.additional_properties)
    for key in _EXCLUSIONS:
        props.pop(key, None)
    group = dict(props.get(GROUP_ANNOTATION_KEY, {}))
    for key in _LINKS:
        group.pop(key, None)
    props.update(overlay)
    props[GROUP_ANNOTATION_KEY] = {**group, **overlay[GROUP_ANNOTATION_KEY]}
    return kept


def _generated(message: Message) -> bool:
    return _is_written_record(message) or _is_marker(message) or _is_summary(message)


def _insertable(message: Message) -> bool:
    return len(message.contents) == 1 and message.contents[0].type == "text" and _generated(message)


def save_view(carried: CarriedView, decisions: dict[str, Any]) -> dict[str, Any] | None:
    """Detach a cache from its messages and bind it to the decisions that produced it."""
    if not carried.portable or not carried.matches_completed_view():
        return None
    positions = {id(m): i for i, m in enumerate(carried.standing) if m is not None}
    if len(positions) != sum(m is not None for m in carried.standing):
        return None
    sources: list[Any] = []
    order: list[dict[str, Any]] = []
    required = max(carried.replaced, default=-1) + 1
    try:
        for i, items in enumerate(carried.contents):
            digest = carried.digests[i]
            if digest is None:
                return None
            member = carried.standing[i]
            accepted = [carried.states[i]]
            known_links = [carried.links[i]]
            if i in carried.replaced:
                accepted.append(carried.replaced[i])
            if member is None:
                required = max(required, i + 1)
            else:
                if not _same_contents(items, member.contents):
                    return None
                accepted.append(_host_state(member))
                known_links.append(_links(member))
                if (
                    accepted[-1] != accepted[0]
                    or known_links[-1]
                    or known_links[0]
                    or member.additional_properties.get(EXCLUDED_KEY)
                    or member.additional_properties.get(PRESERVED_KEY)
                    or _generated(member)
                ):
                    required = max(required, i + 1)
            flags = {_flags(dict(state[3])) for state in accepted}
            if any(reason in _OWN_EXCLUSIONS for _, reason in flags):
                flags.add((False, None))
            sources.append(
                [
                    digest,
                    sorted({_state(s) for s in accepted}),
                    sorted(flags, key=canonical_json),
                    sorted({json_digest(value) for value in known_links}),
                ]
            )
        previous = -1
        for message in carried.view:
            position = positions.get(id(message))
            if position is None:
                if not _insertable(message):
                    return None
                order.append({"insert": message.to_dict()})
                required = max(required, previous + 1)
            else:
                if position <= previous:
                    return None
                previous = position
                order.append(
                    {"source": position, "id": message.message_id, "overlay": _overlay(message)}
                )
        # JSON detaches the layout from live annotations and lets a store carry it opaquely.
        layout = canonical_json({"sources": sources, "order": order, "required": required})
        return {"version": _VERSION, "layout": layout, "digest": _layout_digest(decisions, layout)}
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return None


def resume_saved_view(
    saved: dict[str, Any],
    decisions: dict[str, Any],
    messages: list[Message],
    current: Handed,
) -> Resumed | None:
    """Fit the layout to this turn without mutating the caller's list on a cache miss."""
    try:
        if saved["version"] != _VERSION or saved["digest"] != _layout_digest(
            decisions, saved["layout"]
        ):
            return None
        match = _match(json.loads(saved["layout"]), current)
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, RecursionError):
        return None
    if match is None:
        return None
    view, paired, consumed = match
    omitted = set(range(consumed)).difference(index for _, _, index in paired)
    hidden = omitted.union(
        at for kept, _, at in paired if kept.additional_properties.get(EXCLUDED_KEY)
    )
    for at in hidden:
        properties = messages[at].additional_properties
        if properties.get(PRESERVED_KEY) and properties.get(PRESERVE_REASON_KEY) not in _OWN_HOLDS:
            return None
    return apply_view(view, messages, current, paired, consumed, omitted=omitted)


def _flags(properties: dict[str, Any]) -> tuple[bool, str | None]:
    return properties.get(EXCLUDED_KEY, False), properties.get(EXCLUDE_REASON_KEY)


def _match(
    layout: dict[str, Any],
    current: Handed,
) -> tuple[list[Message], list[tuple[Message, Message, int]], int] | None:
    messages = current.messages
    sources = layout["sources"]
    order: list[dict[str, Any]] = layout["order"]
    inserted: dict[int, Message] = {}
    by_digest: defaultdict[str, list[int]] = defaultdict(list)
    source_slots: dict[int, int] = {}
    for index, entry in enumerate(order):
        if "insert" in entry:
            message = Message.from_dict(entry["insert"])
            if not _insertable(message):
                return None
            inserted[index] = message
            digest = content_digest(message.contents)
            if digest is None:
                return None
            by_digest[digest].append(index)
        else:
            source_slots[entry["source"]] = index
    matched: list[int] = []
    consumed = 0
    from_store: dict[int, int] = {}
    last_slot = -1
    for index, message in enumerate(messages):
        position = len(matched)
        digest = current.digests[index]
        if digest is None:
            return None
        if position < len(sources) and digest == sources[position][0]:
            _, accepted, flags, known_links = sources[position]
            props = message.additional_properties
            if (
                _state(current.states[index]) not in accepted
                or list(_flags(props)) not in flags
                or json_digest(current.links[index]) not in known_links
            ):
                return None
            matched.append(index)
            slot = source_slots.get(position)
        elif digest in by_digest:
            candidates = by_digest[digest]
            if len(candidates) != 1 or not _generated(message):
                return None
            slot = candidates.pop()
            previous = inserted[slot]
            if (
                _state(current.states[index]) != _state(_host_state(previous))
                or (message.message_id is not None and message.message_id != previous.message_id)
                or current.links[index] != _links(previous)
                or not _insert_flags_match(message, previous)
            ):
                return None
            from_store[slot] = index
            inserted[slot] = _standing(message, previous.message_id, _overlay(previous))
        else:
            break
        if slot is not None:
            if slot <= last_slot:
                return None
            last_slot = slot
        consumed = index + 1
    if not matched or len(matched) < layout["required"]:
        return None
    if len({id(message) for message in messages}) != len(messages):
        return None
    if any(
        _generated(messages[at]) or current.digests[at] in by_digest
        for at in range(consumed, len(messages))
    ):
        return None
    rebuilt: list[Message] = []
    paired: list[tuple[Message, Message, int]] = []
    taken = {m.message_id for m in messages if m.message_id}
    for index, entry in enumerate(order):
        if index in inserted:
            member = inserted[index]
            if index not in from_store and member.message_id in taken:
                return None
            # On a positional collision the framework allocates from the length of the
            # list at insertion. A fresh replay on a longer history assigns a different ID.
            mid = member.message_id
            if (
                index not in from_store
                and mid
                and not durable_message_id(mid)
                and int(mid[4:]) > index
            ):
                return None
            if (
                index not in from_store
                and member.additional_properties.get(PRESERVE_REASON_KEY) in _OWN_HOLDS
            ):
                member.additional_properties.pop(PRESERVED_KEY, None)
                member.additional_properties.pop(PRESERVE_REASON_KEY)
            if index in from_store:
                at = from_store[index]
                paired.append((member, messages[at], at))
            rebuilt.append(member)
        elif entry["source"] < len(matched):
            at = matched[cast(int, entry["source"])]
            source = messages[at]
            if source.message_id and source.message_id != entry["id"]:
                return None
            kept = _standing(source, entry["id"], entry["overlay"])
            paired.append((kept, source, at))
            rebuilt.append(kept)
    return rebuilt, paired, consumed


def _insert_flags_match(message: Message, inserted: Message) -> bool:
    flags = _flags(message.additional_properties)
    saved = _flags(inserted.additional_properties)
    return flags == saved or (flags == (False, None) and saved[1] in _OWN_EXCLUSIONS)
