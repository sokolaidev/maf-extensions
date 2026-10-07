"""Data structures shared by the compaction / prompt-cache benchmark."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from agent_framework import Message

__all__ = [
    "CellKey",
    "CellSummary",
    "Transcript",
    "TranscriptTurn",
    "TurnRecord",
]


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    """One scripted model call.

    ``request`` messages are appended to history immediately before the model call and
    ``reply`` messages immediately after it. The model's real answer is discarded so that
    providers and strategies replay the same script, apart from per-cell cache-isolation salts.
    """

    request: tuple[Message, ...]
    reply: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class Transcript:
    """A deterministic scripted conversation used as benchmark input."""

    name: str
    system: Message
    turns: tuple[TranscriptTurn, ...]
    approx_final_prompt_tokens: int

    @property
    def message_count(self) -> int:
        """Total messages in the transcript once fully replayed."""
        return 1 + sum(len(turn.request) + len(turn.reply) for turn in self.turns)


@dataclass(frozen=True, slots=True)
class CellKey:
    """Identifies a single benchmark cell.

    A cell is one full replay of one transcript against one provider under one
    compaction strategy. Every cell gets its own cache namespace via a unique salt,
    so cells never share provider-side cache entries.
    """

    provider: str
    model: str
    transcript: str
    strategy: str
    repeat: int

    @property
    def label(self) -> str:
        """Human-readable identifier used in logs and reports."""
        return f"{self.provider}/{self.transcript}/{self.strategy}#{self.repeat}"


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """Measurements captured for a single model call.

    ``*_local`` fields come from this package's own prefix oracle and are always
    available. The remaining token fields come from the provider's usage report and
    are ``None`` when the provider does not report them.
    """

    cell: CellKey
    turn: int
    history_messages: int
    sent_messages: int
    sent_tokens_local: int
    reusable_prefix_tokens_local: int
    prefix_broken: bool
    input_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a flat, JSON-serializable mapping of this record."""
        data = asdict(self)
        cell: dict[str, Any] = data.pop("cell")
        return {**cell, **data}


@dataclass(frozen=True, slots=True)
class CellSummary:
    """Aggregated statistics for one benchmark cell."""

    cell: CellKey
    reports_cache_tokens: bool
    turns: int
    errors: int
    prefix_breaks: int
    turns_missing_input: int
    total_input_tokens: int
    total_cached_tokens: int
    total_output_tokens: int
    total_local_sent_tokens: int
    total_local_reusable_tokens: int
    mean_latency_ms: float | None
    p50_latency_ms: float | None

    @property
    def fresh_input_tokens(self) -> int:
        """Input tokens the provider had to prefill, billed at the full rate."""
        return max(self.total_input_tokens - self.total_cached_tokens, 0)

    @property
    def cache_hit_ratio(self) -> float | None:
        """Fraction of reported input tokens that were served from the provider cache.

        Suppressed when any turn reported cached tokens without an input count: some
        providers omit ``input_token_count`` on a cache hit, which would understate the
        denominator and inflate this ratio past 100%.
        """
        if not self.reports_cache_tokens or self.total_input_tokens <= 0:
            return None
        if self.turns_missing_input:
            return None
        return self.total_cached_tokens / self.total_input_tokens

    @property
    def local_reusable_ratio(self) -> float | None:
        """Fraction of sent tokens that were an unchanged prefix of the previous call.

        This is the theoretical ceiling on the cache hit ratio: it measures how much of
        each prompt the compaction strategy left untouched, independent of whether the
        provider actually cached it.
        """
        if self.total_local_sent_tokens <= 0:
            return None
        return self.total_local_reusable_tokens / self.total_local_sent_tokens

    @property
    def cache_realization(self) -> float | None:
        """How much of the reusable prefix the provider actually served from cache.

        Computed as ``cache_hit_ratio / local_reusable_ratio`` — a quotient of two
        dimensionless fractions, deliberately *not* ``total_cached / total_reusable``.
        Those two totals are counted in different units: cached tokens come from the
        provider's tokenizer, reusable tokens from this package's local estimator, which
        runs roughly 2x higher because it measures serialized JSON. Dividing them directly
        halves the result and makes every provider look far worse at caching than it is.

        A value near 1.0 means the provider cached everything prefix theory says it could.
        Well below 1.0 means misses compaction does not explain: eviction, TTL expiry,
        a minimum-size floor, or a provider that engages caching only intermittently.
        """
        hit_ratio = self.cache_hit_ratio
        reusable_ratio = self.local_reusable_ratio
        if hit_ratio is None or not reusable_ratio:
            return None
        return hit_ratio / reusable_ratio

    def effective_input_tokens(self, cache_read_ratio: float) -> float:
        """Return input tokens weighted by a provider's cached-token billing ratio.

        Args:
            cache_read_ratio: Price of one cached input token relative to a fresh one.
                For example ``0.25`` for a provider that bills cache reads at 25%.

        Returns:
            Fresh input tokens plus discounted cached input tokens.
        """
        return self.fresh_input_tokens + self.total_cached_tokens * cache_read_ratio

    def to_dict(self, *, cache_read_ratio: float | None = None) -> dict[str, Any]:
        """Return a flat, JSON-serializable mapping of this summary.

        Args:
            cache_read_ratio: When provided, adds an ``effective_input_tokens`` entry
                computed at that billing ratio.

        Returns:
            A mapping of cell identity, raw totals, and derived ratios.
        """
        data = asdict(self)
        cell: dict[str, Any] = data.pop("cell")
        derived: dict[str, Any] = {
            "fresh_input_tokens": self.fresh_input_tokens,
            "cache_hit_ratio": self.cache_hit_ratio,
            "local_reusable_ratio": self.local_reusable_ratio,
            "cache_realization": self.cache_realization,
        }
        if cache_read_ratio is not None:
            derived["effective_input_tokens"] = self.effective_input_tokens(cache_read_ratio)
        return {**cell, **data, **derived}
