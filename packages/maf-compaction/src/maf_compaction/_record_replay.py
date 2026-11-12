"""Source-bound authority for replaying accepted record replacements."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final

from agent_framework import Message
from agent_framework._compaction import EXCLUDED_KEY

from ._preserve import STANDS_IN_KEY
from ._shed import durable_message_id

LINEAGE_KEY: Final[str] = "maf_compaction_record_lineage"


@dataclass
class RecordEdit:
    """An accepted record replacement and the edits its source window already contained."""

    sources: str
    count: int
    text: str | None
    summaries: list[str]
    shed_ids: list[str]
    shed_groups: list[str]


def source_key(messages: list[Message], groups: list[dict[str, Any]]) -> str | None:
    """Identify a replay's source window and originally excluded later reasoning."""
    key = source_lineage(messages, groups)
    if key is None:
        return None
    excluded_reasoning = {
        _reasoning_key(message)
        for message in messages[groups[-1]["end_index"] + 1 :]
        if message.additional_properties.get(EXCLUDED_KEY, False)
        and any(content.type == "text_reasoning" for content in message.contents)
    }
    if None in excluded_reasoning:
        # Content cannot identify old reasoning once unrelated metadata changes.
        return None
    return "v2:" + key + ":" + ",".join(sorted(value for value in excluded_reasoning if value))


def source_lineage(messages: list[Message], groups: list[dict[str, Any]]) -> str | None:
    """Fingerprint identified sources independently of later history."""
    return _source_key(messages, groups, {})


def _message_key(message: Message) -> str:
    payload = [
        message.role,
        message.author_name,
        message.message_id
        if durable_message_id(message.message_id)
        and not message.additional_properties.get(STANDS_IN_KEY)
        and not message.additional_properties.get(LINEAGE_KEY)
        else None,
        message.additional_properties.get(STANDS_IN_KEY),
        message.additional_properties.get(LINEAGE_KEY),
        bool(message.additional_properties.get(EXCLUDED_KEY, False)),
        [content.to_dict(exclude={"raw_representation"}) for content in message.contents],
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _reasoning_key(message: Message) -> str | None:
    if durable_message_id(message.message_id):
        return hashlib.sha256(str(message.message_id).encode()).hexdigest()
    return None


def _source_key(
    messages: list[Message], groups: list[dict[str, Any]], message_keys: dict[int, str]
) -> str | None:
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
    for index in range(start, end + 1):
        if index not in message_keys:
            message_keys[index] = _message_key(messages[index])
    payload = [
        [[group["start_index"] - start, group["end_index"] - start] for group in groups],
        [message_keys[index] for index in range(start, end + 1)],
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def replay_match(
    messages: list[Message],
    groups: list[dict[str, Any]],
    accepted: Collection[tuple[str, int]],
    *,
    message_keys: dict[int, str] | None = None,
) -> tuple[list[dict[str, Any]], tuple[str, int]] | None:
    """Find an unambiguous accepted batch; shared fingerprints require an unchanged history."""
    matches: dict[tuple[str, int], list[list[dict[str, Any]]]] = {}
    if message_keys is None:
        message_keys = {}
    certificates: dict[tuple[str, int], list[tuple[str, set[str]]]] = {}
    for certificate, count in accepted:
        version, _, payload = certificate.partition(":")
        key, separator, excluded = payload.partition(":")
        if version == "v2" and separator and count > 0:
            certificates.setdefault((key, count), []).append(
                (certificate, set(excluded.split(",")) if excluded else set())
            )
    included_reasoning = {
        index: _reasoning_key(message)
        for index, message in enumerate(messages)
        if not message.additional_properties.get(EXCLUDED_KEY, False)
        and any(content.type == "text_reasoning" for content in message.contents)
    }
    for count in sorted({count for _, count in certificates}, reverse=True):
        for start in range(len(groups) - count + 1):
            run = groups[start : start + count]
            key = _source_key(messages, run, message_keys)
            if key is None:
                continue
            for certificate, excluded in certificates.get((key, count), []):
                if not any(
                    identity in excluded
                    for index, identity in included_reasoning.items()
                    if index > run[-1]["end_index"]
                ):
                    matches.setdefault((certificate, count), []).append(run)
    candidates = [(runs[0], key) for key, runs in matches.items() if len(runs) == 1]
    return min(
        candidates,
        key=lambda match: (match[0][0]["start_index"], -len(match[0])),
        default=None,
    )
