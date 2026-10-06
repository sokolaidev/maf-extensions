"""The local prefix oracle and cell aggregation.

Provider prompt caches match on exact prefixes, so a compaction strategy's effect on
caching is entirely determined by how much of the previous prompt survives unchanged at
the front of the next one. This module computes that quantity locally, which serves two
purposes: it is the theoretical ceiling every provider is measured against, and it is the
*only* available signal for providers such as Ollama that report no cache statistics.

Matching is done at message granularity. A message that changed even slightly contributes
zero reusable tokens, so the oracle is deliberately conservative: it never claims more
reuse than a provider could actually deliver.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from agent_framework import Content, Message, TokenizerProtocol

from ._types import CellKey, CellSummary, TurnRecord

__all__ = [
    "clamp_cached_tokens",
    "common_message_prefix",
    "percentile",
    "serialize_message",
    "summarize_cell",
    "token_counts",
]

_NON_WIRE_CONTENT_KEYS: tuple[str, ...] = ("raw_representation", "items")


def _serialize_content(content: Content) -> dict[str, Any]:
    """Return the wire-relevant fields of a content item."""
    payload = content.to_dict(exclude_none=True)
    for key in _NON_WIRE_CONTENT_KEYS:
        payload.pop(key, None)
    return payload


def serialize_message(message: Message) -> str:
    """Return a canonical string identifying everything a provider would see for a message.

    ``message_id`` and compaction's own annotations live on the message but are never
    sent to the provider, so they are excluded. Two messages with equal serializations are
    indistinguishable to the provider and therefore mutually cacheable.

    Args:
        message: The message to serialize.

    Returns:
        A stable, order-independent JSON encoding of the message role and contents.
    """
    payload = {
        "role": message.role,
        "contents": [_serialize_content(content) for content in message.contents],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def common_message_prefix(previous: Sequence[str], current: Sequence[str]) -> int:
    """Return how many leading messages are identical between two serialized projections.

    Args:
        previous: Serialized messages sent on the previous turn.
        current: Serialized messages being sent on this turn.

    Returns:
        The count of leading messages that match exactly.
    """
    limit = min(len(previous), len(current))
    for index in range(limit):
        if previous[index] != current[index]:
            return index
    return limit


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Return the linear-interpolated percentile of ``values``.

    Args:
        values: Sample values. May be unsorted. An empty sequence yields ``None``.
        fraction: Percentile expressed in the range 0.0 to 1.0.

    Returns:
        The requested percentile, or ``None`` when there are no values.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def token_counts(messages: Sequence[Message], *, tokenizer: TokenizerProtocol) -> list[int]:
    """Return the estimated token count of each message.

    Args:
        messages: Messages to measure.

    Keyword Args:
        tokenizer: Token counter applied to each serialized message.

    Returns:
        One token count per message, in order.
    """
    return [tokenizer.count_tokens(serialize_message(message)) for message in messages]


def _has_usable_usage(record: TurnRecord) -> bool:
    """Require positive reported input for a non-empty request."""
    if record.input_tokens is not None and record.input_tokens < 0:
        return False
    return record.sent_tokens_local <= 0 or (record.input_tokens or 0) > 0


def clamp_cached_tokens(input_tokens: int, cached_tokens: int | None) -> int:
    """Keep cache reads within the input tokens of the request they belong to."""
    return min(max(cached_tokens or 0, 0), max(input_tokens, 0))


def summarize_cell(
    records: Sequence[TurnRecord], *, cell: CellKey, reports_cache_tokens: bool
) -> CellSummary:
    """Aggregate every turn of one cell into a single summary row.

    Turns that failed contribute to ``errors`` but not to any token or latency total, so
    a partially failed cell still yields usable ratios over the turns that succeeded.

    Args:
        records: Every turn record belonging to this cell.

    Keyword Args:
        cell: Identity of the cell being summarized.
        reports_cache_tokens: Whether the provider reports cache statistics at all. When
            False, cache-derived ratios are suppressed rather than reported as zero.

    Returns:
        The aggregated summary.
    """
    completed = [record for record in records if record.error is None]
    successful = [record for record in completed if _has_usable_usage(record)]
    latencies = [record.latency_ms for record in successful if record.latency_ms is not None]
    return CellSummary(
        cell=cell,
        reports_cache_tokens=reports_cache_tokens,
        turns=len(records),
        errors=len(records) - len(completed),
        prefix_breaks=sum(1 for record in records if record.prefix_broken),
        # Some providers drop input_token_count on a cache hit while still reporting the
        # cached count. Counting those turns keeps the hit ratio from being computed
        # against a denominator the provider never sent.
        turns_missing_input=len(completed) - len(successful),
        total_input_tokens=sum(record.input_tokens or 0 for record in successful),
        total_cached_tokens=sum(
            clamp_cached_tokens(record.input_tokens or 0, record.cached_tokens)
            for record in successful
        ),
        total_output_tokens=sum(record.output_tokens or 0 for record in successful),
        total_local_sent_tokens=sum(record.sent_tokens_local for record in completed),
        total_local_reusable_tokens=sum(
            record.reusable_prefix_tokens_local for record in completed
        ),
        mean_latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
        p50_latency_ms=percentile(latencies, 0.5),
    )
