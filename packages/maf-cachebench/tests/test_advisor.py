"""Unit tests for the cost-based strategy recommendation.

All offline: summaries are constructed directly so the verdict logic can be exercised
against the exact shapes measured in practice, including the erratic providers.
"""

from __future__ import annotations

import pytest

from maf_cachebench import CellKey, CellSummary, ModelPricing, advise, cost_of

LUNA = ModelPricing(input_per_million=0.20, cached_read_per_million=0.02)


def _summary(
    strategy: str, *, repeat: int, input_tokens: int, cached: int, reported: bool = True
) -> CellSummary:
    return CellSummary(
        cell=CellKey(provider="p", model="m", transcript="xxl", strategy=strategy, repeat=repeat),
        reports_cache_tokens=reported,
        turns=9,
        errors=0,
        prefix_breaks=0,
        turns_missing_input=0,
        total_input_tokens=input_tokens,
        total_cached_tokens=cached,
        total_output_tokens=0,
        total_local_sent_tokens=input_tokens,
        total_local_reusable_tokens=0,
        mean_latency_ms=1.0,
        p50_latency_ms=1.0,
    )


def test_cost_prices_cached_and_fresh_separately() -> None:
    # 1M input of which 800k cached: 200k at $0.20/M plus 800k at $0.02/M.
    s = _summary("none", repeat=1, input_tokens=1_000_000, cached=800_000)
    assert cost_of(s, LUNA) == pytest.approx(0.2 * 0.2 + 0.8 * 0.02)


def test_unreported_cache_is_priced_as_no_discount() -> None:
    s = _summary("none", repeat=1, input_tokens=1_000_000, cached=0, reported=False)
    assert cost_of(s, LUNA) == pytest.approx(0.20)


def test_recommends_no_compaction_when_the_model_caches_well() -> None:
    # The measured gpt-5.6-luna shape: compaction sends fewer tokens but pays for more of
    # them, so the baseline wins outright.
    summaries = [
        *(_summary("none", repeat=r, input_tokens=1_863_313, cached=1_512_649) for r in (1, 2, 3)),
        *(
            _summary("context_window", repeat=r, input_tokens=1_310_131, cached=626_995)
            for r in (1, 2, 3)
        ),
    ]
    verdict = advise(summaries, LUNA)
    assert verdict.recommended == "none"
    assert verdict.confidence == "high"
    # The verdict must compare the baseline against the cheapest *compacted* option. Using
    # the overall cheapest would make this a zero-difference tie and report "every option
    # lands within 0% of not compacting" while context_window is actually far dearer.
    assert verdict.contender.strategy == "context_window"
    assert verdict.saving_fraction < -0.05
    assert "MORE than not" in verdict.rationale


def test_recommends_compaction_when_the_model_caches_badly() -> None:
    # The measured glm-5.2 shape: a poor baseline hit rate that compaction improves.
    glm = ModelPricing(input_per_million=1.19, cached_read_per_million=0.221)
    summaries = [
        *(_summary("none", repeat=r, input_tokens=1_863_037, cached=701_056) for r in (1, 2, 3)),
        *(
            _summary("truncation", repeat=r, input_tokens=1_542_516, cached=1_353_103)
            for r in (1, 2, 3)
        ),
    ]
    verdict = advise(summaries, glm)
    assert verdict.recommended == "truncation"
    assert verdict.confidence == "high"
    assert verdict.saving_fraction > 0.5


def test_refuses_to_rank_an_erratic_model() -> None:
    # deepseek swung 78% -> 16% hit rate on byte-identical input. A median of that is not
    # a measurement, and ranking on it would invent precision that does not exist.
    summaries = [
        _summary("none", repeat=1, input_tokens=2_039_499, cached=1_600_000),
        _summary("none", repeat=2, input_tokens=1_940_914, cached=317_952),
        _summary("none", repeat=3, input_tokens=2_039_499, cached=1_600_000),
        _summary("truncation", repeat=1, input_tokens=1_689_038, cached=1_106_176),
        _summary("truncation", repeat=2, input_tokens=1_688_880, cached=557_056),
        _summary("truncation", repeat=3, input_tokens=1_689_038, cached=1_106_176),
    ]
    verdict = advise(summaries, ModelPricing(0.04, 0.008))
    assert verdict.confidence == "inconclusive"
    assert "erratic" in verdict.rationale


def test_single_repeat_is_flagged_as_low_confidence() -> None:
    summaries = [
        _summary("none", repeat=1, input_tokens=1_000_000, cached=800_000),
        _summary("truncation", repeat=1, input_tokens=500_000, cached=100_000),
    ]
    verdict = advise(summaries, LUNA)
    assert verdict.confidence == "low"
    assert "--repeats" in verdict.rationale


def test_unreported_cache_lowers_confidence() -> None:
    summaries = [
        *(
            _summary("none", repeat=r, input_tokens=1_000_000, cached=0, reported=False)
            for r in (1, 2)
        ),
        *(
            _summary("truncation", repeat=r, input_tokens=400_000, cached=0, reported=False)
            for r in (1, 2)
        ),
    ]
    verdict = advise(summaries, LUNA)
    assert verdict.confidence == "low"
    assert "no cache statistics" in verdict.rationale


def test_near_identical_costs_are_not_ranked() -> None:
    summaries = [
        *(_summary("none", repeat=r, input_tokens=1_000_000, cached=800_000) for r in (1, 2)),
        *(_summary("truncation", repeat=r, input_tokens=985_000, cached=788_000) for r in (1, 2)),
    ]
    verdict = advise(summaries, LUNA)
    # Within the negligible band, so the baseline stands rather than a coin-flip winner.
    assert verdict.recommended == "none"
    assert "not a reason" in verdict.rationale


def test_missing_baseline_is_an_error() -> None:
    with pytest.raises(ValueError, match="Baseline"):
        advise([_summary("truncation", repeat=1, input_tokens=1000, cached=0)], LUNA)


def test_no_usable_cells_is_an_error() -> None:
    with pytest.raises(ValueError, match="No cells"):
        advise([_summary("none", repeat=1, input_tokens=0, cached=0)], LUNA)


def test_missing_cache_price_means_no_discount_not_a_free_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 142 of OpenRouter's paid models list no input_cache_read. Treating that absence as
    # $0 would price cache reads as free and make caching look perfect on precisely the
    # models that do not cache.
    import maf_cachebench._advisor as advisor

    class _Response:
        @staticmethod
        def raise_for_status() -> None: ...

        @staticmethod
        def json() -> dict[str, object]:
            return {"data": [{"id": "vendor/no-cache", "pricing": {"prompt": "0.000002"}}]}

    monkeypatch.setattr(advisor.httpx, "get", lambda *a, **k: _Response())  # type: ignore[attr-defined]
    pricing = advisor.fetch_openrouter_pricing("vendor/no-cache")
    assert pricing.input_per_million == pytest.approx(2.0)
    assert pricing.cached_read_per_million == pytest.approx(2.0)
    assert pricing.cache_discount == 0.0


def test_declared_cache_price_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    import maf_cachebench._advisor as advisor

    class _Response:
        @staticmethod
        def raise_for_status() -> None: ...

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "data": [
                    {
                        "id": "m",
                        "pricing": {"prompt": "0.0000002", "input_cache_read": "0.00000002"},
                    }
                ]
            }

    monkeypatch.setattr(advisor.httpx, "get", lambda *a, **k: _Response())  # type: ignore[attr-defined]
    pricing = advisor.fetch_openrouter_pricing("m")
    assert pricing.cache_discount == pytest.approx(0.9)


@pytest.mark.parametrize("field", ["errors", "turns_missing_input"])
@pytest.mark.parametrize("strategy", ["none", "truncation"])
def test_incomplete_cells_cannot_enter_cost_ranking(field: str, strategy: str) -> None:
    from dataclasses import replace

    incomplete = replace(_summary(strategy, repeat=2, input_tokens=1, cached=0), **{field: 1})
    summaries = [
        _summary("none", repeat=1, input_tokens=1000, cached=0),
        _summary("truncation", repeat=1, input_tokens=2000, cached=0),
        incomplete,
    ]
    verdict = advise(summaries, LUNA)
    assert verdict.recommended == "none"
    assert all(len(entry.costs) == 1 for entry in verdict.ranked)
    with pytest.raises(ValueError, match="No cells"):
        advise([incomplete], LUNA)


@pytest.mark.parametrize("baseline_repeats,contender_repeats", [(1, 3), (3, 1)])
def test_both_sides_need_repeats_for_high_confidence(
    baseline_repeats: int,
    contender_repeats: int,
) -> None:
    summaries = [
        *(_summary("none", repeat=r, input_tokens=1000, cached=0) for r in range(baseline_repeats)),
        *(
            _summary("truncation", repeat=r, input_tokens=500, cached=0)
            for r in range(contender_repeats)
        ),
    ]
    assert advise(summaries, LUNA).confidence == "low"
