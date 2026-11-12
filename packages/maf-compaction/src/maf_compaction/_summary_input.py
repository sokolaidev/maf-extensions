"""Bound summarizer requests without splitting the units they replace."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, cast

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
    probes = _prefix_probes(len(items), min_items)
    try:
        count = next(probes)
        while True:
            count = probes.send(
                fits_summary_input(prompt, render(items[:count]), tokenizer=tokenizer, limit=limit)
            )
    except StopIteration as done:
        count, extent = cast(tuple[int, int | None], done.value)
        return items[:count], extent


async def async_bounded_prefix_extent[T](
    items: list[T],
    *,
    render: Callable[[list[T]], str],
    prompt: str,
    tokenizer: TokenizerProtocol,
    limit: int,
    min_items: int = 1,
    cooperative: bool = True,
) -> tuple[list[T], int | None]:
    """Run the same prefix search, optionally yielding before each tokenization probe."""
    probes = _prefix_probes(len(items), min_items)
    try:
        count = next(probes)
        while True:
            if cooperative:
                await asyncio.sleep(0)
            count = probes.send(
                fits_summary_input(prompt, render(items[:count]), tokenizer=tokenizer, limit=limit)
            )
    except StopIteration as done:
        count, extent = cast(tuple[int, int | None], done.value)
        return items[:count], extent


def _prefix_probes(size: int, min_items: int) -> Generator[int, bool, tuple[int, int | None]]:
    """Yield sizes to measure and receive whether they fit, retaining the deciding extent."""
    if size < min_items:
        return 0, None
    checked: dict[int, bool] = {}

    def fits(count: int) -> Generator[int, bool, bool]:
        if count not in checked:
            checked[count] = yield count
        return checked[count]

    def found(count: int) -> tuple[int, int | None]:
        return count, None if ended else max(checked)

    lower, upper = min_items - 1, min_items
    while upper < size and (yield from fits(upper)):
        lower, upper = upper, min(upper * 2, size)
    # Stopped by a probe that failed short of the end, every size probed is one a longer list
    # would probe the same way; stopped by the end, a longer list would probe further.
    ended = upper >= size
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if (yield from fits(middle)):
            lower = middle
        else:
            upper = middle - 1
    if lower >= min_items:
        return found(lower)
    # Token counts need not grow with text, so failed probes cannot rule out other sizes.
    for count in range(min_items, size + 1):
        if (yield from fits(count)):
            return found(count)
    return 0, None
