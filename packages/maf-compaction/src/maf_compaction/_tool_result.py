"""Eligibility for replacing tool-result text items."""

from collections.abc import Callable
from copy import copy, deepcopy

from agent_framework import Content, Message

type ResultTextRewriter = Callable[[Content, str], bool]


def _copy_result(content: Content) -> Content:
    """Copy result state without Content's deepcopy dropping provider representations."""
    candidate = copy(content)
    candidate.__dict__ = deepcopy(
        {key: value for key, value in vars(content).items() if key != "items"}
    )
    candidate.items = (
        [_copy_result(item) for item in content.items] if content.items is not None else None
    )
    return candidate


def plan_result_items(
    content: Content, text: str, rewrite_result_text: ResultTextRewriter | None
) -> list[Content] | None:
    """Prepare replacement items without mutating the source result."""
    if can_shorten_result(content):
        return [Content.from_text(text)]
    if rewrite_result_text is None:
        return None
    candidate = _copy_result(content)
    non_text = [item for item in candidate.items or [] if item.type != "text"]
    if not rewrite_result_text(candidate, text):
        return None
    items = candidate.items or []
    if "\n".join(item.text for item in items if item.type == "text" and item.text) != text:
        raise ValueError("rewrite_result_text must set items text to the supplied text.")
    remaining = [item for item in items if item.type != "text"]
    if len(remaining) != len(non_text) or any(
        item is not original for item, original in zip(remaining, non_text, strict=True)
    ):
        raise ValueError("rewrite_result_text must retain non-text items in order.")
    # Reuse the source media so hooks cannot change its payload or provider state.
    originals = iter(item for item in content.items or [] if item.type != "text")
    return [item if item.type == "text" else next(originals) for item in items]


def can_shorten_result(content: Content) -> bool:
    """Return whether replacing result items can leave all metadata intact."""
    plain_text = Content.from_text("").to_dict(exclude={"text"})
    return all(
        item.raw_representation is None and item.to_dict(exclude={"text"}) == plain_text
        for item in content.items or []
    )


def repair_compacted_items(messages: list[Message], is_compacted: Callable[[str], bool]) -> bool:
    """Synchronize bare items with an already compacted compatibility result."""
    changed = False
    for message in messages:
        for content in message.contents:
            if (
                content.type == "function_result"
                and isinstance(content.result, str)
                and content.items
                and is_compacted(content.result)
                and can_shorten_result(content)
                and [item.text for item in content.items] != [content.result]
            ):
                content.items = [Content.from_text(content.result)]
                changed = True
    return changed
