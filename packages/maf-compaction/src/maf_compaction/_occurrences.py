"""Resolve function results against preceding unanswered declarations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_framework import Message

type ContentPosition = tuple[int, int]


def function_result_candidates(
    messages: Sequence[Message],
) -> dict[ContentPosition, tuple[ContentPosition, ...]]:
    """Return possible declarations per result; consume only unambiguous matches."""
    pending: dict[str, list[ContentPosition]] = {}
    results: dict[ContentPosition, tuple[ContentPosition, ...]] = {}
    for message_index, message in enumerate(messages):
        if message.role not in ("assistant", "tool"):
            continue
        for content_index, content in enumerate(message.contents):
            if not content.call_id:
                continue
            position = (message_index, content_index)
            if message.role == "assistant" and content.type == "function_call":
                pending.setdefault(content.call_id, []).append(position)
            elif content.type == "function_result":
                calls = pending.get(content.call_id, [])
                candidates = tuple(
                    call
                    for call in calls
                    if not content.id or messages[call[0]].contents[call[1]].id == content.id
                )
                results[position] = candidates
                if len(candidates) == 1:
                    calls.remove(candidates[0])
    return results
