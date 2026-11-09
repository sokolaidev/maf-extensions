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
    return bounded_prefix_extent(
        items, render=render, prompt=prompt, tokenizer=tokenizer, limit=limit, min_items=min_items
    )[0]


def bounded_prefix_extent[T](
    items: list[T],
    *,
    render: Callable[[list[T]], str],
    prompt: str,
    tokenizer: TokenizerProtocol,
    limit: int,
    min_items: int = 1,
) -> tuple[list[T], int | None]:
    """Return :func:`bounded_prefix`'s prefix and how many leading items decided it.

    The count is None when where ``items`` ends decided it as well, so that items appended to the
    list could change the prefix; otherwise any list starting with that many of the same items
    has the same prefix.
    """
    if len(items) < min_items:
        return [], None
    checked: dict[int, bool] = {}

    def fits(count: int) -> bool:
        if count not in checked:
            checked[count] = fits_summary_input(
                prompt, render(items[:count]), tokenizer=tokenizer, limit=limit
            )
        return checked[count]

    def found(count: int) -> tuple[list[T], int | None]:
        return items[:count], None if ended else max(checked)

    lower, upper = min_items - 1, min_items
    while upper < len(items) and fits(upper):
        lower, upper = upper, min(upper * 2, len(items))
    # Stopped by a probe that failed short of the end, every size probed is one a longer list
    # would probe the same way; stopped by the end, a longer list would probe further.
    ended = upper >= len(items)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if fits(middle):
            lower = middle
        else:
            upper = middle - 1
    if lower >= min_items:
        return found(lower)
    # Token counts need not grow with text, so failed probes cannot rule out other sizes.
    for count in range(min_items, len(items) + 1):
        if fits(count):
            return found(count)
    return [], None
