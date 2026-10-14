"""Media estimates and stable identities for compaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import copy, deepcopy
from typing import Any, cast

from agent_framework import Content, Message, TokenizerProtocol
from agent_framework._compaction import (
    GROUP_ANNOTATION_KEY,
    GROUP_TOKEN_COUNT_KEY,
)
from agent_framework._compaction import (
    annotate_token_counts as framework_annotate_token_counts,
)

MEDIA_TYPES = frozenset({"data", "uri", "hosted_file"})
_MEDIA_COUNTED = "_maf_media_counted"


def media_parts(content: Content) -> list[Content]:
    """Find media in a part or its tool-result items."""
    if content.type in MEDIA_TYPES:
        return [content]
    return [part for item in content.items or [] for part in media_parts(item)]


def media_identity(message: Message) -> str:
    """Fingerprint the source and surrounding turn without persisting its media payload."""
    payload = [
        message.message_id,
        [part.to_dict(exclude={"raw_representation"}) for part in message.contents],
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class MediaTokenEstimator:
    """Add configurable media costs to message estimates used by this package.

    Text counting delegates to ``tokenizer``. Media payloads and references are replaced
    by a small marker plus the estimate, never tokenized as URLs or base64. Pass this same
    object to every strategy and recall middleware; costs are estimates, not provider usage.
    """

    def __init__(
        self,
        tokenizer: TokenizerProtocol,
        *,
        image_tokens: int = 1024,
        other_media_tokens: int = 1024,
        estimate_media: Callable[[Content], int] | None = None,
    ) -> None:
        """Configure fixed costs or a per-part override for all supported media types."""
        for cost in (image_tokens, other_media_tokens):
            if type(cost) is not int or cost <= 0:
                raise ValueError("Media token estimates must be positive integers.")
        self.tokenizer = tokenizer
        self.image_tokens = image_tokens
        self.other_media_tokens = other_media_tokens
        self.estimate_media = estimate_media

    def count_tokens(self, text: str) -> int:
        """Count ordinary text with the wrapped tokenizer."""
        return self.tokenizer.count_tokens(text)

    def count_message_tokens(self, message: Message) -> int:
        """Count a message with media replaced by configured estimates."""
        shadow, media_cost = _prepare_message(message, self)
        framework_annotate_token_counts([shadow], tokenizer=self.tokenizer, force_retokenize=True)
        return (
            int(shadow.additional_properties[GROUP_ANNOTATION_KEY][GROUP_TOKEN_COUNT_KEY])
            + media_cost
        )


def _prepare_message(message: Message, estimator: MediaTokenEstimator) -> tuple[Message, int]:
    media_cost = 0

    def replace(content: Content) -> Content:
        nonlocal media_cost
        if content.type in MEDIA_TYPES:
            cost = (
                estimator.estimate_media(content)
                if estimator.estimate_media is not None
                else estimator.image_tokens
                if (content.media_type or "").lower().startswith("image/")
                else estimator.other_media_tokens
            )
            if type(cost) is not int or cost <= 0:
                raise ValueError("Media token estimates must be positive integers.")
            media_cost += cost
            return Content.from_text("[media]")
        if content.type == "function_result" and any(
            media_parts(item) for item in content.items or []
        ):
            replaced = copy(content)
            replaced.items = [replace(item) for item in content.items or []]
            # Items are the sent representation; result is its compatibility mirror.
            replaced.result = [item.to_dict() for item in replaced.items]
            return replaced
        return content

    shadow = copy(message)
    shadow.contents = [replace(content) for content in message.contents]
    shadow.additional_properties = deepcopy(message.additional_properties)
    annotation = shadow.additional_properties.get(GROUP_ANNOTATION_KEY)
    if isinstance(annotation, dict):
        cast(dict[str, Any], annotation).pop(GROUP_TOKEN_COUNT_KEY, None)
    return shadow, media_cost


def annotate_token_counts(
    messages: list[Message],
    *,
    tokenizer: TokenizerProtocol,
    from_index: int | None = None,
    force_retokenize: bool = False,
) -> None:
    """Retain incremental text counts, recomputing media costs without accumulating them."""
    estimator = (
        tokenizer if isinstance(tokenizer, MediaTokenEstimator) else MediaTokenEstimator(tokenizer)
    )
    shadows: list[Message] = []
    costs: dict[int, int] = {}
    for index, message in enumerate(messages):
        if any(media_parts(content) for content in message.contents):
            shadow, cost = _prepare_message(message, estimator)
            shadows.append(shadow)
            costs[index] = cost
            if from_index is not None:
                from_index = min(from_index, index)
        else:
            if message.additional_properties.pop(_MEDIA_COUNTED, False):
                force_retokenize = True
            shadows.append(message)
    framework_annotate_token_counts(
        shadows,
        tokenizer=tokenizer,
        from_index=from_index,
        force_retokenize=force_retokenize,
    )
    for index, cost in costs.items():
        annotation = shadows[index].additional_properties[GROUP_ANNOTATION_KEY]
        annotation[GROUP_TOKEN_COUNT_KEY] += cost
        messages[index].additional_properties[GROUP_ANNOTATION_KEY] = annotation
        messages[index].additional_properties[_MEDIA_COUNTED] = True
