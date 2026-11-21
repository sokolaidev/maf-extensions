"""Media estimates and stable identities for compaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import copy, deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

from agent_framework import Content, Message, TokenizerProtocol
from agent_framework._compaction import (
    GROUP_ANNOTATION_KEY,
    GROUP_TOKEN_COUNT_BASIS_KEY,
    GROUP_TOKEN_COUNT_KEY,
    TOKEN_COUNT_BASIS_VERSION,
    _token_count,  # pyright: ignore[reportPrivateUsage]
    annotate_message_groups,
)
from agent_framework._compaction import (
    annotate_token_counts as framework_annotate_token_counts,
)

MEDIA_TYPES = frozenset({"data", "uri", "hosted_file"})
_MEDIA_COUNTED = "_maf_media_counted"
MESSAGE_COUNTED = "_maf_message_counted"


def _message_counter(tokenizer: TokenizerProtocol) -> Callable[[Message], int] | None:
    """Return the tokenizer's optional whole-message counter."""
    counter = getattr(tokenizer, "count_message_tokens", None)
    return cast(Callable[[Message], int], counter) if callable(counter) else None


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
        count = _message_counter(self.tokenizer)
        if count is not None and not any(media_parts(content) for content in message.contents):
            return count(message)
        shadow, media_cost = _prepare_message(message, self)
        if count is not None:
            return count(shadow) + media_cost
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


@dataclass
class TokenCounts:
    """Share message token counts across the candidate histories of one replay call.

    Counting from an index has the framework serialize and count every message behind it,
    and a replay inserts one record for each edit it puts back, so a long history is counted
    once per edit. A count is kept here under what the framework serializes to take it: the
    role, the message id and the contents, by the list a copied message shares with its
    original. Each entry holds that list and its items, and is taken again when they differ.

    It must not outlive one replay call, for the reason
    :class:`~._record_replay.MessageKeyCache` gives: the compaction around a replay rewrites
    a result in place, which no identity shows, and nothing a replay itself calls does.
    """

    entries: dict[tuple[Any, ...], tuple[Any, list[Content], tuple[Content, ...], int]] = field(
        default_factory=dict[tuple[Any, ...], tuple[Any, list[Content], tuple[Content, ...], int]]
    )

    def annotate(
        self,
        shadows: list[Message],
        messages: list[Message],
        *,
        tokenizer: TokenizerProtocol,
        from_index: int,
    ) -> None:
        """Count from ``from_index`` as the framework does, reading what this call counted.

        ``shadows`` is what the framework would be handed: ``messages``, with each media
        message replaced by its text-only stand-in.
        """
        if not shadows:
            return
        annotate_message_groups(shadows, from_index=from_index)
        start = max(0, min(from_index, len(shadows) - 1))
        if not all(shadow.message_id for shadow in shadows[start:]):
            # Unreachable after grouping; a count taken apart would number the message anew.
            framework_annotate_token_counts(shadows, tokenizer=tokenizer, from_index=from_index)
            return
        for index in range(start, len(shadows)):
            shadow = shadows[index]
            annotation = shadow.additional_properties.get(GROUP_ANNOTATION_KEY)
            if not isinstance(annotation, Mapping):
                continue
            annotation = cast(dict[str, Any], annotation)
            annotation[GROUP_TOKEN_COUNT_KEY] = self._count(shadow, messages[index], tokenizer)
            annotation[GROUP_TOKEN_COUNT_BASIS_KEY] = TOKEN_COUNT_BASIS_VERSION
            shadow.additional_properties[GROUP_ANNOTATION_KEY] = annotation

    def _count(self, shadow: Message, message: Message, tokenizer: TokenizerProtocol) -> int:
        contents = message.contents
        fields: tuple[Any, ...] = (shadow.role, shadow.message_id)
        # Equal values of different types serialize differently; those are counted afresh.
        if not all(value is None or isinstance(value, str) for value in fields):
            return _text_tokens(shadow, tokenizer)
        slot = (*fields, shadow is not message, id(contents), id(tokenizer))
        entry = self.entries.get(slot)
        if (
            entry is not None
            and len(entry[2]) == len(contents)
            and all(held is content for held, content in zip(entry[2], contents, strict=True))
        ):
            return entry[3]
        count = _text_tokens(shadow, tokenizer)
        self.entries[slot] = (tokenizer, contents, tuple(contents), count)
        return count


def _text_tokens(message: Message, tokenizer: TokenizerProtocol) -> int:
    """Return the framework's count for one message, taken on a copy."""
    probe = copy(message)
    probe.additional_properties = dict[str, Any]()
    framework_annotate_token_counts([probe], tokenizer=tokenizer, force_retokenize=True)
    return int(probe.additional_properties[GROUP_ANNOTATION_KEY][GROUP_TOKEN_COUNT_KEY])


def annotate_token_counts(
    messages: list[Message],
    *,
    tokenizer: TokenizerProtocol,
    from_index: int | None = None,
    force_retokenize: bool = False,
    counts: TokenCounts | None = None,
) -> None:
    """Retain incremental text counts, recomputing media costs without accumulating them.

    ``counts`` shares one replay call's counts between its candidate histories; it is read
    only for a count from an index, which is the one that repeats.
    """
    count = structural_counter(tokenizer)
    if count is not None:
        start = (
            0
            if force_retokenize or from_index is None
            else max(0, min(from_index, len(messages) - 1))
        )
        for index, message in enumerate(messages[:start]):
            previous = message.additional_properties.get(MESSAGE_COUNTED)
            if previous is None or previous != _token_count(message):
                start = index
                break
        annotate_message_groups(messages, from_index=None if from_index is None else start)
        for message in messages[start:]:
            # The hook may read fields outside the framework's serialization and replay cache.
            annotation = message.additional_properties[GROUP_ANNOTATION_KEY]
            tokens = count(message)
            annotation[GROUP_TOKEN_COUNT_KEY] = tokens
            annotation[GROUP_TOKEN_COUNT_BASIS_KEY] = TOKEN_COUNT_BASIS_VERSION
            message.additional_properties[MESSAGE_COUNTED] = tokens
        return

    estimator = (
        tokenizer if isinstance(tokenizer, MediaTokenEstimator) else MediaTokenEstimator(tokenizer)
    )
    shadows: list[Message] = []
    costs: dict[int, int] = {}
    previously_structural: list[Message] = []
    for index, message in enumerate(messages):
        if MESSAGE_COUNTED in message.additional_properties:
            previously_structural.append(message)
            force_retokenize = True
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
    if counts is not None and from_index is not None and not force_retokenize:
        counts.annotate(shadows, messages, tokenizer=tokenizer, from_index=from_index)
    else:
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
    for message in previously_structural:
        message.additional_properties.pop(MESSAGE_COUNTED, None)


def structural_counter(tokenizer: TokenizerProtocol) -> Callable[[Message], int] | None:
    """Keep the framework's incremental accounting for a media estimator over a text tokenizer."""
    count = _message_counter(tokenizer)
    if (
        isinstance(tokenizer, MediaTokenEstimator)
        and getattr(count, "__func__", None) is MediaTokenEstimator.count_message_tokens
        and _message_counter(tokenizer.tokenizer) is None
    ):
        return None
    return count
