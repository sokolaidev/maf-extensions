"""Eligibility for replacing tool-result text items."""

from agent_framework import Content


def can_shorten_result(content: Content) -> bool:
    """Return whether replacing result items can leave all metadata intact."""
    plain_text = Content.from_text("").to_dict(exclude={"text"})
    return all(
        item.raw_representation is None and item.to_dict(exclude={"text"}) == plain_text
        for item in content.items or []
    )
