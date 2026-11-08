"""Durable identities for replaying whole-group shedding."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from agent_framework import Message
from agent_framework._compaction import group_messages


def durable_message_id(message_id: str | None) -> bool:
    """Exclude framework-generated positional IDs from persisted removal authority."""
    return bool(message_id and not re.fullmatch(r"msg_[0-9]+", message_id))


def unique_shed_groups(messages: list[Message]) -> list[tuple[str, list[Message]]]:
    """Fingerprint complete groups without message IDs or mutable compaction annotations.

    Repeated identical groups are ambiguous and cannot authorize content-based replay.
    """
    groups: list[tuple[str, list[Message]]] = []
    for group in group_messages(messages):
        members = messages[group["start_index"] : group["end_index"] + 1]
        payload = [
            [
                message.role,
                message.author_name,
                [part.to_dict(exclude={"raw_representation"}) for part in message.contents],
            ]
            for message in members
        ]
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        groups.append((key, members))
    counts = Counter(key for key, _ in groups)
    return [(key, members) for key, members in groups if counts[key] == 1]
