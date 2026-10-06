"""Token counters for compaction budgets and the local prefix oracle.

``build_tokenizer`` wraps the selected text counter with replayed-reasoning accounting.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Final, cast

from agent_framework import CharacterEstimatorTokenizer, Message, TokenizerProtocol

__all__ = [
    "REASONING_TOKENS_KEY",
    "TOKENIZER_NAMES",
    "ReasoningStampTokenizer",
    "TiktokenTokenizer",
    "build_tokenizer",
    "stamp_reasoning_tokens",
]

TOKENIZER_NAMES: Final[tuple[str, ...]] = ("estimator", "tiktoken")

#: ``additional_properties`` key under which a response's billed reasoning token count is
#: recorded on the content that carries its encrypted payload. See :func:`stamp_reasoning_tokens`.
REASONING_TOKENS_KEY: Final[str] = "reasoning_output_token_count"

_STAMP_MARKER: Final[str] = f'"{REASONING_TOKENS_KEY}"'

#: The member a provider's replayed reasoning item keeps its encrypted payload under. The
#: framework drops it from the top level of a content's ``additional_properties``; this
#: wrapper drops it at any depth below, where the Foundry client keeps a copy of the item.
_OPAQUE_KEY: Final[str] = "encrypted_content"
_OPAQUE_MARKER: Final[str] = f'"{_OPAQUE_KEY}"'


class TiktokenTokenizer:
    """Exact BPE token counts, so budgets and reuse are measured in real tokens."""

    def __init__(self, encoding: str = "o200k_base") -> None:
        """Create a tokenizer.

        Args:
            encoding: A ``tiktoken`` encoding name. ``o200k_base`` covers current OpenAI
                models and is a reasonable proxy for other vendors' counts.

        Raises:
            RuntimeError: If ``tiktoken`` is not installed.
        """
        try:
            import tiktoken
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise RuntimeError("The 'tiktoken' tokenizer requires the tiktoken package.") from error
        self._encoding = tiktoken.get_encoding(encoding)

    def count_tokens(self, text: str) -> int:
        """Return the exact number of BPE tokens in ``text``."""
        return len(self._encoding.encode(text))


class ReasoningStampTokenizer:
    """Count serialized messages with replayed reasoning at its stamped token count.

    Remove reasoning stamps and nested ``encrypted_content`` from content properties
    before counting text, then add the stamped totals. Clear-text replay content stays
    counted; strings without either marker or without a message's ``contents`` list
    pass through unchanged.
    """

    def __init__(self, base: TokenizerProtocol) -> None:
        """Wrap a token counter.

        Args:
            base: The counter that measures the message text; whichever the run selected.
        """
        self.base = base

    def count_tokens(self, text: str) -> int:
        """Return the wrapped count of ``text`` without its stamps, plus what the stamps declare.

        Args:
            text: The string to count, normally a message as ``_serialize_message`` emits it.

        Returns:
            The wrapped tokenizer's count of the re-serialized message plus the stamped
            reasoning token counts; unchanged when there are no stamps or opaque payloads.
        """
        if _STAMP_MARKER not in text and _OPAQUE_MARKER not in text:
            return self.base.count_tokens(text)
        settled, declared = _settle_for_count(text)
        if settled is None:
            return self.base.count_tokens(text)
        return self.base.count_tokens(settled) + declared


def _settle_for_count(text: str) -> tuple[str | None, int]:
    """Return ``text`` re-serialized without stamps or nested opaque payloads, plus the stamp total.

    Args:
        text: A string that contains the stamp key or the opaque payload key.

    Returns:
        ``(None, 0)`` when ``text`` is not a serialized message with a ``contents`` list, or
        when no entry of that list carries a stamp or an opaque payload in its
        ``additional_properties``, so the caller counts ``text`` unchanged. Otherwise the
        re-serialized message and the sum of the stamps that are positive integers; a stamp of
        any other shape is removed and counts nothing.
    """
    try:
        payload: Any = json.loads(text)
    except ValueError:
        return None, 0
    if not isinstance(payload, dict):
        return None, 0
    contents: Any = cast("dict[str, Any]", payload).get("contents")
    if not isinstance(contents, list):
        return None, 0
    changed = False
    declared = 0
    for entry in cast("list[Any]", contents):
        if not isinstance(entry, dict):
            continue
        properties = cast("dict[str, Any]", entry).get("additional_properties")
        if not isinstance(properties, dict):
            continue
        typed = cast("dict[str, Any]", properties)
        if REASONING_TOKENS_KEY in typed:
            stamp = typed.pop(REASONING_TOKENS_KEY)
            changed = True
            if isinstance(stamp, int) and not isinstance(stamp, bool):
                declared += max(stamp, 0)
        if _drop_opaque(typed):
            changed = True
    if not changed:
        return None, 0
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), declared


def _drop_opaque(value: Any) -> bool:
    """Remove every ``encrypted_content`` member below ``value`` in place; say whether any went."""
    dropped = False
    if isinstance(value, dict):
        entries = cast("dict[str, Any]", value)
        if _OPAQUE_KEY in entries:
            del entries[_OPAQUE_KEY]
            dropped = True
        for item in entries.values():
            dropped = _drop_opaque(item) or dropped
    elif isinstance(value, list):
        for item in cast("list[Any]", value):
            dropped = _drop_opaque(item) or dropped
    return dropped


def stamp_reasoning_tokens(messages: Iterable[Message], reasoning_tokens: int) -> bool:
    """Record a response's reasoning token count on the content carrying its encrypted payload.

    The count goes under :data:`REASONING_TOKENS_KEY` in the ``additional_properties`` of the
    first content in ``messages`` whose ``protected_data`` is set. A response normally carries
    one payload; when it carries several the total sits on the first, which is what
    :class:`ReasoningStampTokenizer` counts, since it sums stamps across contents.

    This changes the message but not what is sent: the OpenAI and Foundry reasoning
    conversion reads ``status``, ``reasoning_text`` and ``encrypted_content`` from those
    properties and nothing else. It does change the message's serialized string, for the
    framework's count and for the benchmark's prefix oracle alike, from the first prompt the
    message appears in onwards, so a stamped message is as cacheable as an unstamped one.

    Args:
        messages: The response's messages. The in-memory history provider stores the
            response's own ``Content`` objects, so a stamp applied after the call reaches
            the replayed message.
        reasoning_tokens: ``UsageDetails["reasoning_output_token_count"]`` for the response.

    Returns:
        Whether a content was stamped: False when ``reasoning_tokens`` is not positive or no
        content carries a payload.
    """
    if reasoning_tokens <= 0:
        return False
    for message in messages:
        for content in message.contents:
            if content.protected_data is not None:
                content.additional_properties[REASONING_TOKENS_KEY] = reasoning_tokens
                return True
    return False


def build_tokenizer(name: str) -> TokenizerProtocol:
    """Build a token counter by name.

    Args:
        name: One of :data:`TOKENIZER_NAMES`.

    Returns:
        The token counter, wrapped in :class:`ReasoningStampTokenizer`.

    Raises:
        KeyError: If ``name`` is not a known tokenizer.
    """
    base: TokenizerProtocol
    if name == "estimator":
        base = CharacterEstimatorTokenizer()
    elif name == "tiktoken":
        base = TiktokenTokenizer()
    else:
        raise KeyError(f"Unknown tokenizer {name!r}. Known tokenizers: {list(TOKENIZER_NAMES)}")
    return ReasoningStampTokenizer(base)
