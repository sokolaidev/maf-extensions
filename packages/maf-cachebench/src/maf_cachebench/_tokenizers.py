"""Token counters used for compaction budgets and the local prefix oracle.

The default ``CharacterEstimatorTokenizer`` assumes 4 chars/token over serialized JSON,
which runs roughly 2x a real BPE count for this benchmark's content. That is harmless when
comparing strategies at small sizes, but at 100k-plus prompts it moves a compaction
threshold by six figures -- so large runs should count real tokens.

Whichever counter a run selects, :func:`build_tokenizer` hands it back inside
:class:`ReasoningStampTokenizer`, so that the reasoning a provider bills on every replay of
an encrypted payload is counted at the size the provider reports rather than at zero. That
class documents the accounting.
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
    """Count a serialized message as the provider bills it, replayed reasoning included.

    **What the framework counts.** ``agent_framework._compaction._serialize_message`` is what
    every compaction strategy hands the tokenizer, and it excludes the opaque reasoning payload
    a provider returns with ``protected_data`` (an encrypted Responses item, an Anthropic
    signature) while keeping any clear-text reasoning the provider replays as text. So a
    replayed encrypted reasoning item counts as zero. The provider does not bill it at zero:
    it bills the decrypted reasoning, about 300 tokens per assistant call on gpt-5.6-luna.
    Nothing on the message says how many that is -- the count arrives on the response's usage
    as ``reasoning_output_token_count``, not on the content -- so on a conversation with ~38
    assistant calls in the prompt the framework's count runs about 11k tokens, roughly 19%,
    under what the provider bills, and every threshold in this package is a fraction of that
    count: a trigger labelled 0.80 fires near 0.99 of billed.

    **What this does.** A run stamps each response's reasoning count on the content carrying
    its payload (:func:`stamp_reasoning_tokens`). The framework keeps ``additional_properties``
    in the string it counts, so the stamp travels with the message. When the string carries a
    stamp this wrapper parses it once, removes every stamp from the ``contents`` entries, counts
    the re-serialized message with the wrapped tokenizer -- the same call ``_serialize_message``
    makes (``ensure_ascii=False, sort_keys=True, default=str``) -- and adds the stamps' total.
    The stamp itself is never counted and the replayed reasoning is counted at what the provider
    reports; the residual is the framing the provider puts around a replayed item, a few tokens
    per call. A string with no stamp, or one that is not a serialized message with a
    ``contents`` list, goes to the wrapped tokenizer unchanged, so every message on a model
    that does not reason, and every user, tool and plain assistant message on one that does,
    counts exactly as the framework would count it alone.

    **The payload the framework misses.** The framework excludes ``protected_data`` and the
    ``encrypted_content`` member at the top level of a content's ``additional_properties``, and
    nothing below that. The Foundry client keeps a copy of the whole replayed reasoning item
    under one key of those properties, ``__foundry_reasoning_replay_item__``, with the encrypted
    payload inside it, so on gpt-5.6-luna every assistant message is counted with its base64
    payload again: a reply of 416 visible characters serialized to 2,214 and counted at 1,128
    o200k tokens against about 110 billed, measured 5 October 2026 on agent-framework-core
    1.20.0 and agent-framework-foundry 1.14.0. Every compaction threshold in this package is a
    fraction of that count, so the anchored rows shed assistant turns one after another on a
    prompt a fifth under the window. This wrapper therefore also drops every ``encrypted_content``
    member at any depth under a content's ``additional_properties`` before counting, which is
    the framework's own rule applied where the framework does not apply it. Clear-text members
    of the replayed item -- its summary and content lists -- stay counted, as the framework
    counts them.

    **Cost.** A message without a stamp pays one substring scan and the wrapped count of the
    original string object. A message with one pays a ``json.loads`` and a ``json.dumps`` on
    top, a few microseconds beside tiktoken's hundreds for the same string.
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
            reasoning token counts; or its count of ``text`` itself when there is no stamp.
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
