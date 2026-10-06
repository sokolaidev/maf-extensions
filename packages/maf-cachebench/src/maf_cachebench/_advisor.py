"""Pick the cheapest compaction strategy for one model, and say how sure we are.

Compaction is usually assumed to save money. It does not always: a model whose prompt
cache already covers most of each request loses more to the broken prefix than it gains
from the shorter prompt. Which way it falls is a property of the model, so the only
reliable way to know is to price both options against that model directly.

This module runs a strategy sweep for one model, prices its token usage, and returns a verdict.
It withholds recommendations when repeat variability is wider than the gap between options.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from ._metrics import clamp_cached_tokens, percentile

if TYPE_CHECKING:
    from ._types import CellSummary

__all__ = [
    "ModelPricing",
    "StrategyCost",
    "Verdict",
    "advise",
    "fetch_openrouter_pricing",
]

#: Relative spread across repeats above which a strategy's cost is treated as unstable.
#: Set from measurement: the reproducible models varied by well under 1%, while the noisy
#: ones varied by 100% or more, so anything past a quarter is firmly in the noisy camp.
UNSTABLE_SPREAD: Final[float] = 0.25

#: Cost difference below which two strategies are called equivalent rather than ranked.
NEGLIGIBLE_SAVING: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """What a model charges per million tokens."""

    input_per_million: float
    cached_read_per_million: float
    output_per_million: float = 0.0
    """Generation rate. Zero for the replay benchmarks, which cap output at a few tokens
    and study only the prompt side; a live run generates real replies and must price them."""
    cache_write_per_million: float | None = None
    """Rate for a prompt token the provider writes into its cache, ``None`` when it charges none.

    Some models bill the uncached part of a prompt above the input rate, because that is the
    part written into the cache for the next call to read -- gpt-6-luna at 1.25x. Pricing it at
    the input rate would under-charge exactly what compaction causes, a broken prefix, and so
    flatter the strategies that break it most. With automatic caching every uncached prompt
    token is taken as written, which is what the provider bills when the prefix is long enough to
    cache at all; a prompt too short to cache is billed at the input rate instead and is
    over-charged here by the premium, which on this benchmark's prompts does not arise."""
    long_context_threshold: int | None = None
    """Input tokens above which a whole request is billed at :attr:`long_context`'s rates.

    Some models price by request size: gpt-6-luna bills a request with more than 272,000 input
    tokens at its long-context rates for every token of it, cached reads, cache writes and
    output included, not only for the tokens past the line. ``None`` for a model with one rate.
    """
    long_context: ModelPricing | None = None
    """The rates a request above :attr:`long_context_threshold` is billed at, ``None`` for none."""

    def __post_init__(self) -> None:
        if self.long_context_threshold is not None and self.long_context_threshold <= 0:
            raise ValueError("long_context_threshold must be greater than 0.")
        for name in (
            "input_per_million",
            "cached_read_per_million",
            "output_per_million",
            "cache_write_per_million",
        ):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative.")

    @property
    def cache_discount(self) -> float:
        """Fraction off the input price that a cache hit earns, 0.0 when there is none."""
        if self.input_per_million <= 0:
            return 0.0
        return max(0.0, 1.0 - self.cached_read_per_million / self.input_per_million)

    def input_cost(self, input_tokens: int, cached_tokens: int) -> float:
        """Return what a prompt cost, cached and uncached together and output excluded.

        Here rather than at each caller because a total and its input half that were computed
        by two expressions could disagree about the cached share, and the whole use of the
        input half is that it is the same money read on one axis.

        Args:
            input_tokens: Every token billed on the prompt side, cached ones included.
            cached_tokens: How many of those the provider served from its cache.

        Returns:
            Cost in the pricing's currency units.
        """
        cached_tokens = clamp_cached_tokens(input_tokens, cached_tokens)
        fresh = max(input_tokens - cached_tokens, 0)
        return (
            fresh * self.fresh_per_million + cached_tokens * self.cached_read_per_million
        ) / 1_000_000

    def tier(self, input_tokens: int) -> ModelPricing:
        """Return the rates one request of this many input tokens is billed at."""
        if (
            self.long_context is not None
            and self.long_context_threshold is not None
            and input_tokens > self.long_context_threshold
        ):
            return self.long_context
        return self

    def tiered_input_cost(
        self,
        input_tokens: int,
        cached_tokens: int,
        *,
        long_input_tokens: int = 0,
        long_cached_tokens: int = 0,
    ) -> float:
        """Return what a run's prompt side cost, when some of its requests were long-context.

        Args:
            input_tokens: Every input token the run billed, long-context requests included.
            cached_tokens: How many of those were served from cache.

        Keyword Args:
            long_input_tokens: The input tokens of the requests above the threshold.
            long_cached_tokens: How many of those were served from cache.

        Returns:
            Cost in the pricing's currency units.
        """
        rates = self.long_context or self
        return self.input_cost(
            input_tokens - long_input_tokens, cached_tokens - long_cached_tokens
        ) + (rates.input_cost(long_input_tokens, long_cached_tokens))

    def tiered_cost(
        self,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        *,
        long_input_tokens: int = 0,
        long_cached_tokens: int = 0,
        long_output_tokens: int = 0,
    ) -> float:
        """Return what a run cost, prompt and output, splitting its long-context requests out.

        Args:
            input_tokens: Every input token billed.
            cached_tokens: How many of those were served from cache.
            output_tokens: Every output token billed.

        Keyword Args:
            long_input_tokens: Input tokens of the requests above the threshold.
            long_cached_tokens: How many of those were served from cache.
            long_output_tokens: Output tokens of those requests.

        Returns:
            Cost in the pricing's currency units.
        """
        rates = self.long_context or self
        return (
            self.tiered_input_cost(
                input_tokens,
                cached_tokens,
                long_input_tokens=long_input_tokens,
                long_cached_tokens=long_cached_tokens,
            )
            + (output_tokens - long_output_tokens) * self.output_per_million / 1_000_000
            + long_output_tokens * rates.output_per_million / 1_000_000
        )

    @property
    def fresh_per_million(self) -> float:
        """Return the rate an uncached prompt token is billed at: the cache-write rate when set."""
        return (
            self.cache_write_per_million
            if self.cache_write_per_million is not None
            else self.input_per_million
        )

    def describe(self) -> str:
        """Return the rates as the reports print them, the cache-write rate only when set."""
        text = (
            f"${self.input_per_million:.2f}/M in, ${self.cached_read_per_million:.3f}/M cached, "
            f"${self.output_per_million:.2f}/M out"
        )
        if self.cache_write_per_million is not None:
            text += (
                f", ${self.cache_write_per_million:.3f}/M cache write "
                "(charged on every uncached input token)"
            )
        if self.long_context is not None and self.long_context_threshold is not None:
            text += (
                f"; a request over {self.long_context_threshold:,} input tokens bills whole at "
                f"{self.long_context.describe()}"
            )
        return text


@dataclass(frozen=True, slots=True)
class StrategyCost:
    """Measured cost of one strategy across its repeats."""

    strategy: str
    costs: tuple[float, ...]
    total_input_tokens: int
    total_cached_tokens: int
    cache_reported: bool

    @property
    def median(self) -> float:
        """Median cost across repeats, the figure the verdict ranks on."""
        return percentile(self.costs, 0.5) or 0.0

    @property
    def spread(self) -> float:
        """Relative gap between the cheapest and dearest repeat.

        Zero when a single repeat was run, which is why the advisor warns separately about
        having nothing to measure stability with.
        """
        if not self.costs or self.median <= 0:
            return 0.0
        return (max(self.costs) - min(self.costs)) / self.median

    @property
    def hit_rate(self) -> float | None:
        """Share of input tokens served from cache, or None when unreported."""
        if not self.cache_reported or self.total_input_tokens <= 0:
            return None
        return self.total_cached_tokens / self.total_input_tokens


@dataclass(frozen=True, slots=True)
class Verdict:
    """The recommendation, its confidence, and the reasoning behind it."""

    recommended: str
    baseline: StrategyCost
    best: StrategyCost
    contender: StrategyCost
    ranked: tuple[StrategyCost, ...]
    confidence: str
    rationale: str

    @property
    def saving_fraction(self) -> float:
        """What the cheapest compaction option saves against not compacting.

        Negative means every compaction option is dearer than leaving it off.
        """
        if self.baseline.median <= 0:
            return 0.0
        return (self.baseline.median - self.contender.median) / self.baseline.median


def cost_of(summary: CellSummary, pricing: ModelPricing) -> float:
    """Return what one replay of a cell costs in input charges.

    Cached tokens use the read rate; fresh ones use the cache-write rate when set. Output is
    ignored: the benchmark caps generation at a handful of tokens because only the prompt
    side is under study.

    Args:
        summary: One cell's measured usage.
        pricing: The model's per-million rates.

    Returns:
        Cost in the pricing's currency units.
    """
    cached = (
        clamp_cached_tokens(summary.total_input_tokens, summary.total_cached_tokens)
        if summary.reports_cache_tokens
        else 0
    )
    return pricing.input_cost(summary.total_input_tokens, cached)


def _collect(summaries: list[CellSummary], pricing: ModelPricing) -> list[StrategyCost]:
    """Group cell summaries by strategy and price each group."""
    grouped: dict[str, list[CellSummary]] = {}
    for summary in summaries:
        grouped.setdefault(summary.cell.strategy, []).append(summary)
    collected: list[StrategyCost] = []
    for strategy, cells in grouped.items():
        usable = [
            cell
            for cell in cells
            if cell.turns > 0
            and cell.total_input_tokens > 0
            and not cell.errors
            and not cell.turns_missing_input
        ]
        if not usable:
            continue
        collected.append(
            StrategyCost(
                strategy=strategy,
                costs=tuple(cost_of(cell, pricing) for cell in usable),
                total_input_tokens=sum(cell.total_input_tokens for cell in usable),
                total_cached_tokens=sum(
                    clamp_cached_tokens(cell.total_input_tokens, cell.total_cached_tokens)
                    for cell in usable
                ),
                cache_reported=all(cell.reports_cache_tokens for cell in usable),
            )
        )
    return collected


def advise(
    summaries: list[CellSummary], pricing: ModelPricing, *, baseline: str = "none"
) -> Verdict:
    """Rank strategies by measured cost and recommend one.

    Args:
        summaries: Every cell measured for a single model.
        pricing: That model's rates.

    Keyword Args:
        baseline: Strategy representing "no compaction", which every other option is
            judged against.

    Returns:
        The verdict, whose ``confidence`` is ``"inconclusive"`` when the measurements
        cannot support a recommendation.

    Raises:
        ValueError: If priced cells lack the baseline or a compaction alternative.
    """
    collected = _collect(summaries, pricing)
    if not collected:
        raise ValueError("No cells with usable token counts to price.")
    by_name = {entry.strategy: entry for entry in collected}
    if baseline not in by_name:
        raise ValueError(f"Baseline strategy {baseline!r} is missing; measured: {sorted(by_name)}")

    base = by_name[baseline]
    ranked = tuple(sorted(collected, key=lambda entry: entry.median))
    best = ranked[0]

    # The comparison that decides the verdict is baseline versus the cheapest *compacted*
    # option — not baseline versus the overall cheapest. When the baseline already wins,
    # those are the same entry and their difference is zero, which would otherwise be
    # reported as "every option ties with not compacting" even though the alternatives
    # might be 50% dearer.
    compacted = [entry for entry in collected if entry.strategy != baseline]
    if not compacted:
        raise ValueError("At least one compaction strategy is required for a comparison.")
    contender = min(compacted, key=lambda entry: entry.median)

    # A cost gap smaller than repeat variability cannot support a ranking.
    worst_spread = max(base.spread, contender.spread)
    single_sample = min(len(base.costs), len(contender.costs)) < 2
    saving = (base.median - contender.median) / base.median if base.median > 0 else 0.0

    if not base.cache_reported or not contender.cache_reported:
        confidence = "low"
        rationale = (
            "Some repeats reported no cache statistics, so their cost assumes no discount. "
            "Incomplete cache telemetry can bias the comparison in either direction."
        )
    elif single_sample:
        confidence = "low"
        rationale = (
            "Only one repeat per strategy, so nothing measures stability. Re-run with --repeats 3."
        )
    elif worst_spread > UNSTABLE_SPREAD and worst_spread > abs(saving):
        confidence = "inconclusive"
        rationale = (
            f"Repeats of the same strategy varied by {worst_spread:.0%}, which is larger than the "
            f"{abs(saving):.0%} gap between the options. This model's caching is too erratic "
            f"to rank "
            "strategies on cost."
        )
    elif saving > NEGLIGIBLE_SAVING:
        confidence = "high"
        rationale = (
            f"{contender.strategy!r} is {saving:.0%} cheaper than not compacting, well beyond the "
            f"{worst_spread:.0%} spread between repeats."
        )
    elif saving < -NEGLIGIBLE_SAVING:
        confidence = "high"
        rationale = (
            f"The cheapest compaction option, {contender.strategy!r}, costs {-saving:.0%} MORE "
            f"than not "
            f"compacting: this model's cache already covers {base.hit_rate or 0:.0%} of each "
            f"request, and "
            "compaction breaks that discount to save fewer tokens than it forfeits."
        )
    else:
        confidence = "high"
        rationale = (
            f"The cheapest compaction option is within {abs(saving):.0%} of not compacting, so "
            f"cost is not "
            "a reason to choose between them. Decide on context-overflow safety instead."
        )

    recommended = contender.strategy if saving > NEGLIGIBLE_SAVING else base.strategy
    return Verdict(
        recommended=recommended,
        baseline=base,
        best=best,
        contender=contender,
        ranked=ranked,
        confidence=confidence,
        rationale=rationale,
    )


def fetch_openrouter_pricing(model: str, *, timeout: float = 30.0) -> ModelPricing:
    """Look up a model's rates from OpenRouter's public catalogue.

    Args:
        model: An OpenRouter model slug, such as ``openai/gpt-5.6-luna``.

    Keyword Args:
        timeout: Seconds to wait for the catalogue.

    Returns:
        The model's input and cached-read rates per million tokens.

    Raises:
        KeyError: If the slug is not in the catalogue.
        OSError: If the catalogue request fails.
    """
    try:
        response = httpx.get("https://openrouter.ai/api/v1/models", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise OSError(f"OpenRouter catalogue request failed: {error}") from error
    catalogue = cast("dict[str, Any]", response.json())
    for entry in cast("list[dict[str, Any]]", catalogue["data"]):
        if entry["id"] != model:
            continue
        pricing = cast("dict[str, Any]", entry.get("pricing") or {})
        input_price = float(pricing.get("prompt") or 0.0) * 1_000_000
        # A missing input_cache_read means the model advertises no cache discount at all —
        # 142 of OpenRouter's 417 paid models are in that position. Reading the absent field
        # as zero would price cache reads as free, inventing a 100% discount for exactly the
        # models that have none, and biasing the verdict against compacting them.
        cached_raw = pricing.get("input_cache_read")
        cached_price = float(cached_raw) * 1_000_000 if cached_raw is not None else input_price
        output_price = float(pricing.get("completion") or 0.0) * 1_000_000
        return ModelPricing(
            input_per_million=input_price,
            cached_read_per_million=cached_price,
            output_per_million=output_price,
        )
    raise KeyError(f"{model!r} is not in the OpenRouter catalogue.")
