"""Unit tests for the combined cost-and-correctness verdict."""

from __future__ import annotations

import pytest

from maf_cachebench import JointOutcome, PlantedFact, RecallScore, recommend, score_answer
from maf_cachebench._advisor import ModelPricing
from maf_cachebench._summary import relative_correctness
from maf_cachebench._summary_cli import _render

FACTS = tuple(PlantedFact(f"F{i}", "requirement", 1, "") for i in range(10))


def _outcome(strategy: str, *, cost: float, recalled: int, messages_left: int = 10) -> JointOutcome:
    answer = " ".join(fact.marker for fact in FACTS[:recalled])
    prompt = " ".join(fact.marker for fact in FACTS)
    return JointOutcome(
        strategy=strategy,
        cost=cost,
        input_tokens=1_000_000,
        cached_tokens=500_000,
        messages_left=messages_left,
        messages_total=20,
        score=RecallScore(
            outcomes=score_answer(answer, FACTS, prompt),
            answer=answer,
            messages_left=messages_left,
            messages_total=20,
        ),
    )


def test_recommends_the_cheapest_strategy_that_keeps_the_answer_intact() -> None:
    outcomes = [
        _outcome("none", cost=1.00, recalled=10),
        _outcome("truncation", cost=0.70, recalled=10),
        # Cheapest overall, but it destroys most of the answer.
        _outcome("context_window_aggressive", cost=0.20, recalled=3),
    ]
    verdict = recommend(outcomes)
    assert verdict.recommended == "truncation"
    assert verdict.saving_fraction == pytest.approx(0.30)


def test_refuses_to_trade_correctness_for_savings() -> None:
    # The cost axis alone would pick the cheapest, which is reliably the most destructive.
    outcomes = [
        _outcome("none", cost=1.00, recalled=10),
        _outcome("context_window", cost=0.40, recalled=5),
    ]
    verdict = recommend(outcomes)
    assert verdict.recommended == "none"
    assert "overflowing" in verdict.rationale


def test_keeps_the_baseline_when_intact_strategies_cost_more() -> None:
    outcomes = [
        _outcome("none", cost=0.50, recalled=10),
        _outcome("context_window", cost=0.80, recalled=10),
    ]
    verdict = recommend(outcomes)
    assert verdict.recommended == "none"
    assert "costs more" in verdict.rationale


def test_correctness_is_judged_against_the_control_not_a_perfect_score() -> None:
    # The control itself scored 8/10; a strategy at 8/10 has lost nothing.
    base = _outcome("none", cost=1.00, recalled=8)
    same = _outcome("truncation", cost=0.60, recalled=8)
    assert relative_correctness(same, base) == pytest.approx(1.0)
    assert recommend([base, same]).recommended == "truncation"


def test_threshold_is_configurable() -> None:
    outcomes = [
        _outcome("none", cost=1.00, recalled=10),
        _outcome("truncation", cost=0.50, recalled=7),
    ]
    assert recommend(outcomes, min_correctness=0.9).recommended == "none"
    assert recommend(outcomes, min_correctness=0.6).recommended == "truncation"


def test_missing_control_is_an_error() -> None:
    with pytest.raises(ValueError, match="Baseline"):
        recommend([_outcome("truncation", cost=1.0, recalled=10)])


def test_verdict_is_withheld_when_the_control_itself_fails() -> None:
    """A control that already fails the task cannot be a reference.

    Dividing by a control that scored 6% turns a strategy scoring 17% into "300% of the
    control", which reads as a threefold improvement rather than as two bad answers. Measured
    on a model that ignored most of its context: every one of 13 strategies was reported as
    clearing the correctness bar.
    """
    outcomes = [
        _outcome("none", cost=1.0, recalled=0),
        _outcome("truncation", cost=0.5, recalled=1),
    ]

    verdict = recommend(outcomes)

    assert verdict.recommended == "none"
    assert "not a usable reference" in verdict.rationale


@pytest.mark.parametrize("baseline_cost", [0.0, -1.0])
@pytest.mark.parametrize("recalled", [5, 10])
def test_nonpositive_baseline_cost_has_no_saving(baseline_cost: float, recalled: int) -> None:
    verdict = recommend(
        [
            _outcome("none", cost=baseline_cost, recalled=10),
            _outcome("truncation", cost=0.0, recalled=recalled),
        ]
    )
    assert verdict.recommended == "none"
    assert verdict.saving_fraction == 0.0
    if recalled == 5:
        assert "saves 0%" in verdict.rationale


@pytest.mark.parametrize("baseline_cost", [0.0, -1.0])
def test_render_nonpositive_baseline_cost_as_unavailable(baseline_cost: float) -> None:
    verdict = recommend(
        [
            _outcome("none", cost=baseline_cost, recalled=10),
            _outcome("truncation", cost=0.0, recalled=10),
        ]
    )
    text = _render(verdict, ModelPricing(1.0, 0.1), "stub", show_answers=False)
    row = next(line for line in text.splitlines() if line.startswith("truncation"))
    assert row.split()[3] == "n/a"
