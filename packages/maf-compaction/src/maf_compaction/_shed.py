"""Durable identities for replaying whole-group shedding."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from agent_framework import Content, Message
from agent_framework._compaction import group_messages


def durable_message_id(message_id: str | None) -> bool:
    """Exclude framework-generated positional IDs from persisted removal authority."""
    return bool(message_id and not re.fullmatch(r"msg_[0-9]+", message_id))


@dataclass
class ShedGroupKeys:
    """Share group fingerprints across the candidate histories of one replay call.

    A replay asks for every group's fingerprint once per candidate history, and each answer
    serializes the whole conversation, though only the groups an edit touches differ between
    two candidates. A group's fingerprint is kept here under its members' roles, authors and
    contents lists, which a copied message shares with its original. Each entry holds the
    lists and items it was taken from, and is taken again when a list holds other items.

    It must not outlive one replay call, for the reason
    :class:`~._record_replay.MessageKeyCache` gives: the compaction around a replay rewrites
    a result in place, which no identity shows, and nothing a replay itself calls does.
    """

    groups: dict[
        tuple[tuple[Any, Any, int], ...],
        tuple[tuple[tuple[list[Content], tuple[Content, ...]], ...], str],
    ] = field(
        default_factory=dict[
            tuple[tuple[Any, Any, int], ...],
            tuple[tuple[tuple[list[Content], tuple[Content, ...]], ...], str],
        ]
    )

    def key(self, members: list[Message]) -> str:
        # Equal values of different types serialize differently; those are taken afresh.
        fields: list[Any] = [
            value for message in members for value in (message.role, message.author_name)
        ]
        if not all(value is None or isinstance(value, str) for value in fields):
            return _group_key(members)
        slot = tuple(
            (message.role, message.author_name, id(message.contents)) for message in members
        )
        entry = self.groups.get(slot)
        if entry is not None and all(
            len(items) == len(message.contents)
            and all(held is content for held, content in zip(items, message.contents, strict=True))
            for (_, items), message in zip(entry[0], members, strict=True)
        ):
            return entry[1]
        key = _group_key(members)
        self.groups[slot] = (
            tuple((message.contents, tuple(message.contents)) for message in members),
            key,
        )
        return key


def _group_key(members: list[Message]) -> str:
    payload = [
        [
            message.role,
            message.author_name,
            [part.to_dict(exclude={"raw_representation"}) for part in message.contents],
        ]
        for message in members
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def unique_shed_groups(
    messages: list[Message], *, keys: ShedGroupKeys | None = None
) -> list[tuple[str, list[Message]]]:
    """Fingerprint complete groups without message IDs or mutable compaction annotations.

    Repeated identical groups are ambiguous and cannot authorize content-based replay.
    ``keys`` shares the fingerprints of one replay call's candidate histories.
    """
    groups: list[tuple[str, list[Message]]] = []
    for group in group_messages(messages):
        members = messages[group["start_index"] : group["end_index"] + 1]
        groups.append((_group_key(members) if keys is None else keys.key(members), members))
    counts = Counter(key for key, _ in groups)
    return [(key, members) for key, members in groups if counts[key] == 1]
