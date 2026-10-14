"""Eligibility for replacing tool-result text items."""

from collections.abc import Callable

from agent_framework import Content, Message


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
