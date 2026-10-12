"""Bound summarizer requests without splitting the units they replace."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_framework import Message
from agent_framework._compaction import annotate_token_counts, included_token_count

if TYPE_CHECKING:
    from agent_framework import TokenizerProtocol


def summary_request(prompt: str, transcript: str) -> list[Message]:
    return [Message(role="system", contents=[prompt]), Message(role="user", contents=[transcript])]


def fits_summary_input(
    prompt: str, transcript: str, *, tokenizer: TokenizerProtocol, limit: int
) -> bool:
    request = summary_request(prompt, transcript)
    annotate_token_counts(request, tokenizer=tokenizer)
    return included_token_count(request) <= limit


def bounded_prefix[T](
    items: list[T],
    *,
    render: Callable[[list[T]], str],
    prompt: str,
    tokenizer: TokenizerProtocol,
    limit: int,
) -> list[T]:
    """Find a fitting prefix of whole oldest items without retokenizing every prefix."""

    def fits(count: int) -> bool:
        return fits_summary_input(prompt, render(items[:count]), tokenizer=tokenizer, limit=limit)

    lower, upper = 0, min(1, len(items))
    while upper < len(items) and fits(upper):
        lower, upper = upper, min(upper * 2, len(items))
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if fits(middle):
            lower = middle
        else:
            upper = middle - 1
    return items[:lower]
