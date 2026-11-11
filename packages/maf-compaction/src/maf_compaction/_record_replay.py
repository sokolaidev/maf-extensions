"""Source-bound authority for replaying accepted record replacements."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from typing import Any, Final

from agent_framework import Message

from ._preserve import STANDS_IN_KEY
from ._shed import durable_message_id

LINEAGE_KEY: Final[str] = "maf_compaction_record_lineage"


def source_key(messages: list[Message], groups: list[dict[str, Any]]) -> str | None:
    """Fingerprint identified sources and their complete intervening message window."""
    for group in groups:
        members = messages[group["start_index"] : group["end_index"] + 1]
        if not any(
            durable_message_id(message.message_id)
            or message.additional_properties.get(STANDS_IN_KEY)
            or message.additional_properties.get(LINEAGE_KEY)
            or any(
                content.type == "function_call" and content.call_id for content in message.contents
            )
            for message in members
        ):
            return None
    start, end = groups[0]["start_index"], groups[-1]["end_index"]
    payload = [
        [[group["start_index"] - start, group["end_index"] - start] for group in groups],
        [
            [
                message.role,
                message.author_name,
                message.message_id
                if durable_message_id(message.message_id)
                and not message.additional_properties.get(STANDS_IN_KEY)
                and not message.additional_properties.get(LINEAGE_KEY)
                else None,
                message.additional_properties.get(STANDS_IN_KEY),
                message.additional_properties.get(LINEAGE_KEY),
                [content.to_dict(exclude={"raw_representation"}) for content in message.contents],
            ]
            for message in messages[start : end + 1]
        ],
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def replay_match(
    messages: list[Message],
    groups: list[dict[str, Any]],
    accepted: Collection[tuple[str, int]],
) -> tuple[list[dict[str, Any]], tuple[str, int]] | None:
    """Find the earliest complete accepted batch, refusing ambiguous source matches."""
    matches: dict[tuple[str, int], list[list[dict[str, Any]]]] = {}
    for count in sorted({count for _, count in accepted}, reverse=True):
        for start in range(len(groups) - count + 1):
            run = groups[start : start + count]
            key = source_key(messages, run)
            if key is not None and (key, count) in accepted:
                matches.setdefault((key, count), []).append(run)
    candidates = [(runs[0], key) for key, runs in matches.items() if len(runs) == 1]
    return min(
        candidates,
        key=lambda match: (match[0][0]["start_index"], -len(match[0])),
        default=None,
    )
