"""Bound summarizer requests without splitting the units they replace."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_framework import Message
from agent_framework._compaction import included_token_count

from ._media import annotate_token_counts

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
    min_items: int = 1,
) -> list[T]:
    """Find a fitting prefix, checking unprobed sizes if fast probes cannot make progress."""
    if len(items) < min_items:
        return []
    checked: dict[int, bool] = {}

    def fits(count: int) -> bool:
        if count not in checked:
            checked[count] = fits_summary_input(
                prompt, render(items[:count]), tokenizer=tokenizer, limit=limit
            )
        return checked[count]

    lower, upper = min_items - 1, min_items
    while upper < len(items) and fits(upper):
        lower, upper = upper, min(upper * 2, len(items))
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if fits(middle):
            lower = middle
        else:
            upper = middle - 1
    if lower >= min_items:
        return items[:lower]
    # Token counts need not grow with text, so failed probes cannot rule out other sizes.
    for count in range(min_items, len(items) + 1):
        if fits(count):
            return items[:count]
    return []
