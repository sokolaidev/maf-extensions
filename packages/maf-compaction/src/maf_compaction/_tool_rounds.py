"""Recognize independent tool rounds appended to an unchanged, settled history."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from agent_framework import Content, Message, TokenizerProtocol
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    GROUP_TOKEN_COUNT_BASIS_KEY,
    GROUP_TOKEN_COUNT_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    _token_count,  # pyright: ignore[reportPrivateUsage]
    annotate_message_groups,
    group_messages,
    included_token_count,
)

from ._media import annotate_token_counts
from ._preserve import (
    _ATTRIBUTION_KEY,  # pyright: ignore[reportPrivateUsage]
    stands_in,
)
from ._shed import positional_shed_sources, unique_shed_groups
from ._toolsummary import (
    RECALL_TOOL_NAME,
    _droppable_groups_after,  # pyright: ignore[reportPrivateUsage]
    _newest_record_identity,  # pyright: ignore[reportPrivateUsage]
    active_record_groups,
    find_record_index,
)


def _freeze(value: Any) -> Any:
    if value is None or type(value) in (str, bytes):
        return value
    if type(value) in (int, bool, float):
        return (type(value), value)
    if isinstance(value, Content):
        return tuple((key, _freeze(item)) for key, item in vars(value).items() if item is not None)
    if isinstance(value, dict):
        return (
            dict,
            tuple((_freeze(k), _freeze(v)) for k, v in cast(dict[Any, Any], value).items()),
        )
    if isinstance(value, (list, tuple)):
        kind = list if isinstance(value, list) else tuple
        return (kind, tuple(_freeze(v) for v in cast(Sequence[Any], value)))
    # A provider object can change in place without its identity changing.
    return object()


def _state(message: Message, *, check_attribution: bool) -> tuple[Any, ...]:
    props = message.additional_properties
    fields = dict(props)
    fields.pop(_ATTRIBUTION_KEY, None)
    if GROUP_ANNOTATION_KEY in fields:
        annotation = dict(fields[GROUP_ANNOTATION_KEY])
        annotation.pop(GROUP_TOKEN_COUNT_KEY, None)
        annotation.pop(GROUP_TOKEN_COUNT_BASIS_KEY, None)
        fields[GROUP_ANNOTATION_KEY] = annotation
    return (
        message.message_id,
        message.role,
        message.author_name,
        _contents(message),
        _freeze(fields),
        check_attribution and _ATTRIBUTION_KEY in props,
    )


def _contents(message: Message) -> Any:
    try:
        return tuple(_freeze(part) for part in message.contents)
    except RecursionError:
        return object()


def _ordinary_call_id(call: Message, result: Message) -> str | None:
    """A complete ordinary exchange cannot answer a declaration outside its round."""
    if call.role != "assistant" or result.role != "tool" or len(result.contents) != 1:
        return None
    for message in (call, result):
        if message.additional_properties.keys() - {
            _ATTRIBUTION_KEY,
            EXCLUDED_KEY,
            GROUP_ANNOTATION_KEY,
        }:
            return None
        if message.additional_properties.get(EXCLUDED_KEY):
            return None
        annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY, {})
        if any(
            key in annotation
            for key in (
                SUMMARY_OF_GROUP_IDS_KEY,
                SUMMARY_OF_MESSAGE_IDS_KEY,
                SUMMARIZED_BY_SUMMARY_ID_KEY,
            )
        ):
            return None
    calls = [c for c in call.contents if c.type == "function_call"]
    if len(calls) != 1 or any(
        c.type not in {"text", "text_reasoning", "function_call"} or c.items for c in call.contents
    ):
        return None
    declared = calls[0]
    answer = result.contents[0]
    if (
        declared.name == RECALL_TOOL_NAME
        or declared.hosted
        or declared.informational_only
        or answer.type != "function_result"
        or not isinstance(answer.result, str)
        or (
            answer.items
            and (
                len(answer.items) != 1
                or answer.items[0].type != "text"
                or answer.items[0].text != answer.result
                or answer.items[0].items
            )
        )
        or (answer.id and answer.id != declared.id)
        or answer.call_id != declared.call_id
    ):
        return None
    return declared.call_id


@dataclass
class SettledTools:
    """Inputs of a per-call pass whose outgoing history needed no further edit.

    The last tool pair stays outside the prefix total because the store can change where its
    exclusion flags last and which positional ids its token counts include.
    """

    states: list[tuple[tuple[Any, ...], int | None]]
    ids: set[str]
    prefix_tokens: int
    policy: tuple[Any, ...]
    anchor: int | None
    identity: str
    pending: int
    responses: int
    records: int
    triggered: bool
    shed_positions: list[tuple[str, tuple[int, ...]]]
    # Value targets before this position were already captured for the outstanding recall.
    target_end: int = 0
    target_count: int = 0

    @classmethod
    def capture(
        cls,
        messages: list[Message],
        *,
        tokens: int,
        policy: tuple[Any, ...],
        responses: int,
        triggered: bool,
        head: int,
        shed_sources: list[tuple[str, list[Message]]],
    ) -> SettledTools | None:
        if len(messages) < 2 or not _ordinary_call_id(messages[-2], messages[-1]):
            return None
        anchor = find_record_index(messages)
        if anchor is not None and anchor >= len(messages) - 2:
            return None
        groups = group_messages(messages)
        start = len(messages) - 2
        if (
            groups[-1]["start_index"] != start
            or sum(
                not stands_in(messages[g["start_index"] : g["end_index"] + 1]) for g in groups[:-1]
            )
            < head
        ):
            return None
        ids: set[str] = set()
        positions = {id(m): i for i, m in enumerate(messages)}
        if len(positions) != len(messages) or any(
            id(m) not in positions for _, members in shed_sources for m in members
        ):
            return None
        for index, message in enumerate(messages):
            if index == start:
                boundary = messages[start:]
                if any(m.message_id in ids for m in boundary) or any(
                    name in ids
                    for m in boundary
                    for part in m.contents
                    for name in (part.call_id, part.id)
                    if name
                ):
                    return None
            if message.message_id:
                ids.add(message.message_id)
            for part in message.contents:
                if part.type not in {
                    "text",
                    "text_reasoning",
                    "function_call",
                    "function_result",
                } or any(item.type != "text" or item.items for item in part.items or []):
                    return None
                ids.update(value for value in (part.call_id, part.id) if value)
        return cls(
            states=[
                (_state(m, check_attribution=i < start), _token_count(m))
                for i, m in enumerate(messages)
            ],
            ids=ids,
            prefix_tokens=tokens - included_token_count(messages[-2:]),
            policy=policy,
            anchor=anchor,
            identity=_newest_record_identity(messages),
            pending=_droppable_groups_after(messages, anchor),
            responses=responses,
            records=len(active_record_groups(messages)),
            triggered=triggered,
            shed_positions=[
                (key, tuple(positions[id(m)] for m in members)) for key, members in shed_sources
            ],
        )

    def measure(
        self,
        messages: list[Message],
        policy: tuple[Any, ...],
        *,
        tokenizer: TokenizerProtocol,
        budget: int,
        trigger: int,
        shed_ids: set[str],
        shed_groups: set[str],
    ) -> int | None:
        """Measure an independent continuation, or defer to the full pass."""
        start = len(self.states)
        if policy != self.policy or len(messages) < start or (len(messages) - start) % 2:
            return None
        annotate_message_groups(messages)
        # Counting or excluding a tail member must leave the prefix's annotations intact.
        if len({id(m.additional_properties) for m in messages}) != len(messages) or len(
            {id(m.additional_properties[GROUP_ANNOTATION_KEY]) for m in messages}
        ) != len(messages):
            return None
        seen = set[str]()
        for index in range(start, len(messages), 2):
            call, result = messages[index : index + 2]
            identity = _ordinary_call_id(call, result)
            if not identity:
                return None
            ids = {identity, call.message_id, result.message_id}
            ids.update(part.id for part in call.contents if part.id)
            names = {value for value in ids if value}
            if not self.ids.isdisjoint(names) or not seen.isdisjoint(names):
                return None
            seen.update(names)
        count_from = start - 2
        for i, (state, count) in enumerate(self.states):
            if state != _state(messages[i], check_attribution=i < start - 2):
                return None
            if i < count_from and _token_count(messages[i]) != count:
                count_from = i
        annotate_token_counts(messages[count_from:], tokenizer=tokenizer)
        if any(
            _token_count(messages[i]) != self.states[i][1] for i in range(count_from, start - 2)
        ):
            return None
        boundary = messages[start - 2 :]
        tail = messages[start:]
        if any(m.message_id is None for m in tail):
            group_messages(
                tail,
                id_offset=start,
                reserved_ids=[m.message_id for m in messages[:start] if m.message_id],
            )
        tokens = self.prefix_tokens + included_token_count(boundary)
        if (
            tokens > budget
            or self.triggered != (tokens > trigger)
            or any(m.message_id in shed_ids for m in boundary)
            or (shed_groups and any(key in shed_groups for key, _ in unique_shed_groups(boundary)))
        ):
            return None
        return tokens

    def shed_sources(self, messages: list[Message]) -> list[tuple[str, list[Message]]]:
        for i in range(len(self.states), len(messages), 2):
            for key, _ in positional_shed_sources(messages[i : i + 2]):
                self.shed_positions.append((key, (i, i + 1)))
        return [(key, [messages[i] for i in positions]) for key, positions in self.shed_positions]

    def added_rounds(self, messages: list[Message]) -> int:
        return (len(messages) - len(self.states)) // 2

    def extend(self, messages: list[Message], tokens: int, policy: tuple[Any, ...]) -> None:
        added = self.added_rounds(messages)
        self.pending += added
        self.responses += added
        start = len(self.states) - 2
        self.states[start:] = [
            (
                _state(messages[i], check_attribution=i < len(messages) - 2),
                _token_count(messages[i]),
            )
            for i in range(start, len(messages))
        ]
        for message in messages[start:]:
            if message.message_id:
                self.ids.add(message.message_id)
            for part in message.contents:
                self.ids.update(value for value in (part.call_id, part.id) if value)
        self.prefix_tokens = tokens - included_token_count(messages[-2:])
        self.policy = policy
