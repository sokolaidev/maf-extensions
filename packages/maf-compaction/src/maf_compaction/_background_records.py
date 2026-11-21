"""The record bodies, token counts and source fingerprints a speculative merge can use."""

from __future__ import annotations

import hashlib
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_framework import Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    GROUP_TOKEN_COUNT_BASIS_KEY,
    GROUP_TOKEN_COUNT_KEY,
    TOKEN_COUNT_BASIS_VERSION,
    annotate_message_groups,
    group_messages,
    included_token_count,
)

from ._media import annotate_token_counts
from ._record_replay import _message_key, source_lineage  # pyright: ignore[reportPrivateUsage]
from ._shed import durable_message_id
from ._toolsummary import (
    RECALL_TOOL_NAME,
    _consolidatable_record_groups,  # pyright: ignore[reportPrivateUsage]
    _is_written_record,  # pyright: ignore[reportPrivateUsage]
    _record_body,  # pyright: ignore[reportPrivateUsage]
    build_record_message,
)

if TYPE_CHECKING:
    from agent_framework import TokenizerProtocol


def has_records(messages: list[Message]) -> bool:
    return any(
        _is_written_record(m)
        or any(c.type == "function_call" and c.name == RECALL_TOOL_NAME for c in m.contents)
        for m in messages
        if m.role == "assistant" and not m.additional_properties.get(EXCLUDED_KEY)
    )


def counted_history(
    history: list[Message], response: list[Message], tokenizer: TokenizerProtocol
) -> list[Message]:
    """Use the foreground's token counts. Own any annotation the response or a new message needs."""
    messages = [*history, *response]
    for index, message in enumerate(messages):
        annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY, {})
        if (
            message.message_id
            and annotation.get(GROUP_TOKEN_COUNT_BASIS_KEY) == TOKEN_COUNT_BASIS_VERSION
            and isinstance(annotation.get(GROUP_TOKEN_COUNT_KEY), int)
        ):
            continue
        owned = copy(message)
        owned.additional_properties = dict(message.additional_properties)
        owned.additional_properties.pop(GROUP_ANNOTATION_KEY, None)
        messages[index] = owned
    # Assign positional ids in the original coordinate system.
    if any(not m.message_id for m in messages):
        group_messages(messages)
    for message in messages:
        if GROUP_ANNOTATION_KEY not in message.additional_properties:
            annotate_token_counts([message], tokenizer=tokenizer)
    return messages


@dataclass
class BackgroundRecords:
    messages: list[Message]
    originals: dict[int, dict[str, Any]]
    held_tokens: int
    exclusions: dict[str, int]

    @classmethod
    def capture(cls, history: list[Message]) -> BackgroundRecords:
        """Collapse each gap to its token count and a fingerprint of its ordered messages."""
        exclusions: dict[str, int] = {}
        groups, recalls, active = _consolidatable_record_groups(history, exclusions=exclusions)
        held = sum(
            included_token_count(history[g["start_index"] : g["end_index"] + 1]) for g in active
        )
        messages: list[Message] = []
        originals: dict[int, dict[str, Any]] = {}
        weights: list[int] = []
        # A later excluded reasoning block is part of the source certificate. Fold its
        # durable id into the suffix once for all preceding records.
        suffix = hashlib.sha256()
        durable = True

        def include_reasoning(message: Message) -> None:
            nonlocal durable
            if message.additional_properties.get(EXCLUDED_KEY) and any(
                content.type == "text_reasoning" for content in message.contents
            ):
                if durable_message_id(message.message_id):
                    suffix.update(hashlib.sha256(str(message.message_id).encode()).digest())
                else:
                    durable = False

        end = len(history)
        for group in reversed(groups):
            last = int(group["end_index"])
            gap = hashlib.sha256()
            tokens = 0
            for index in range(end - 1, last, -1):
                message = history[index]
                tokens += included_token_count([message])
                if messages:
                    gap.update(bytes.fromhex(_message_key(message)))
                include_reasoning(message)
            if end > last + 1:
                messages.append(Message("system", [], message_id="gap-" + gap.hexdigest()))
                weights.append(tokens)
            record = build_record_message(_record_body(history, group, recalls))
            lineage = source_lineage(history, [group])
            if lineage and durable:
                record.message_id = "record-" + lineage + suffix.hexdigest()
            originals[id(record)] = group
            messages.append(record)
            end = int(group["start_index"])
            members = history[end : last + 1]
            for member in reversed(members):
                include_reasoning(member)
            tokens = included_token_count(members)
            held -= tokens
            weights.append(tokens)
        if end:
            messages.append(Message("system", [], message_id="prefix"))
            weights.append(sum(included_token_count([history[i]]) for i in range(end)))
        messages.reverse()
        annotate_message_groups(messages)
        for message, tokens in zip(messages, reversed(weights), strict=True):
            annotation = message.additional_properties[GROUP_ANNOTATION_KEY]
            annotation[GROUP_TOKEN_COUNT_KEY] = tokens
            annotation[GROUP_TOKEN_COUNT_BASIS_KEY] = TOKEN_COUNT_BASIS_VERSION
        return cls(messages, originals, held, exclusions)
