"""Recognize persisted links from generated summaries to their source material."""

from typing import cast

from agent_framework import Message
from agent_framework._compaction import (
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
)


def has_summary_links(message: Message) -> bool:
    """Require nonempty source links that generated summaries and removal notes carry."""
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    if not isinstance(annotation, dict):
        return False
    links = cast(dict[str, object], annotation)
    for key in (SUMMARY_OF_MESSAGE_IDS_KEY, SUMMARY_OF_GROUP_IDS_KEY):
        ids = links.get(key)
        if not isinstance(ids, list):
            return False
        values = cast(list[object], ids)
        if not values or not all(isinstance(value, str) and bool(value) for value in values):
            return False
    return True
