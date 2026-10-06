"""Judge compaction on both axes at once: what it costs and what it destroys.

Measuring the two separately invites the wrong conclusion. The cost sweep says a strategy
is cheap; the recall probe says it is lossy; neither on its own tells you whether to use
it. Worse, running them on different conversations means the numbers describe different
workloads and cannot honestly be placed in the same table.

So both are measured on one conversation: every turn is actually sent, which gives real
token usage across the whole session, and the final turn is scored for whether the model
could still use the facts planted throughout. The recommendation is then the obvious one —
the cheapest strategy that does not degrade the answer — with correctness judged relative
to an uncompacted control rather than against a presumed-perfect 100%, because the model
sometimes overlooks a fact even when everything is in front of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from ._recall import RecallScore

__all__ = ["JointOutcome", "JointVerdict", "recommend"]

#: Fraction of the control's correctness a strategy must retain to be considered usable.
#: Judged relative to the control because an uncompacted run does not always score 100%:
#: a model that overlooks one fact on its own would otherwise make every strategy look
#: worse than it is.
DEFAULT_MIN_CORRECTNESS: Final[float] = 0.9


@dataclass(frozen=True, slots=True)
class JointOutcome:
    """One strategy measured on both axes over the same conversation."""

    strategy: str
    cost: float
    input_tokens: int
    cached_tokens: int
    messages_left: int
    messages_total: int
    score: RecallScore
    correctness_samples: tuple[float, ...] = ()
    """Correctness of each independent reading, when the caller measured more than one.

    A live cell is read several times: every closing question is asked repeatedly of one
    snapshot, and the whole conversation is seeded more than once. Accuracy here is often
    two-valued, so the single ``score`` is a draw from a distribution rather than a summary of
    it, and the verdict has to rank on the distribution's mean or it ranks on a coin toss.
    Empty for the replay paths, which read a cell once.
    """

    @property
    def correctness(self) -> float:
        """Share of correctness checks passed, averaged over every reading that was taken."""
        if self.correctness_samples:
            return fmean(self.correctness_samples)
        return self.score.correctness_score

    @property
    def hit_rate(self) -> float | None:
        """Share of input tokens served from the provider's cache."""
        if self.input_tokens <= 0:
            return None
        return self.cached_tokens / self.input_tokens


@dataclass(frozen=True, slots=True)
class JointVerdict:
    """The recommendation across both axes."""

    recommended: str | None
    """The strategy to use, or None when the control was inadmissible and no row qualified.

    Never the control's name when :attr:`baseline_admissible` is False: not compacting is then
    not an option the model allows, so recommending it would recommend the one row that failed.
    """
    baseline: JointOutcome
    chosen: JointOutcome
    outcomes: tuple[JointOutcome, ...]
    min_correctness: float
    rationale: str
    baseline_admissible: bool = True
    """Whether the control is itself an option, rather than only the accuracy it anchors."""

    @property
    def saving_fraction(self) -> float:
        """What the recommendation saves against not compacting."""
        if self.baseline.cost <= 0:
            return 0.0
        return (self.baseline.cost - self.chosen.cost) / self.baseline.cost


def relative_correctness(outcome: JointOutcome, baseline: JointOutcome) -> float:
    """Return a strategy's correctness as a fraction of the uncompacted control's.

    Args:
        outcome: The strategy being judged.
        baseline: The uncompacted control.

    Returns:
        The ratio, or 0.0 when the control itself scored nothing.
    """
    if baseline.correctness <= 0:
        return 0.0
    return outcome.correctness / baseline.correctness


#: Control correctness below which the relative measure stops meaning anything.
#: Dividing by a control that scored 6% turns a strategy scoring 17% into "300% of the
#: control", which reads as a threefold improvement rather than as two bad answers.
MEANINGFUL_BASELINE: Final[float] = 0.25


def recommend(
    outcomes: list[JointOutcome],
    *,
    baseline: str = "none",
    min_correctness: float = DEFAULT_MIN_CORRECTNESS,
    baseline_admissible: bool = True,
) -> JointVerdict:
    """Recommend the cheapest strategy that keeps the answer intact.

    Cost alone would pick whichever strategy trims hardest, which is reliably the one that
    destroys the most information. Correctness alone would always pick the control. The
    useful question is the constrained one: among the options that still answer correctly,
    which is cheapest.

    Args:
        outcomes: Every strategy measured on the same conversation.

    Keyword Args:
        baseline: The uncompacted control strategy.
        min_correctness: Fraction of the control's correctness a strategy must retain to
            be eligible.
        baseline_admissible: False when the control overflowed the window it stands in for.
            It then still anchors the correctness bar -- it is the only measurement of what
            the whole conversation held -- but it is no longer an option or a price: the
            verdict is the cheapest eligible strategy whatever the control cost, and None when
            nothing is eligible. See :func:`_recommend_without_baseline`.

    Returns:
        The verdict.

    Raises:
        ValueError: If no outcomes were supplied or the control is missing.
    """
    if not outcomes:
        raise ValueError("No measured strategies to compare.")
    by_name = {outcome.strategy: outcome for outcome in outcomes}
    if baseline not in by_name:
        raise ValueError(f"Baseline strategy {baseline!r} is missing; measured: {sorted(by_name)}")

    base = by_name[baseline]
    if not baseline_admissible:
        return _recommend_without_baseline(outcomes, base, min_correctness)
    if base.correctness < MEANINGFUL_BASELINE:
        return JointVerdict(
            recommended=baseline,
            baseline=base,
            chosen=base,
            outcomes=tuple(sorted(outcomes, key=lambda outcome: outcome.cost)),
            min_correctness=min_correctness,
            rationale=(
                f"The uncompacted control scored only {base.correctness:.0%}, so it is not a "
                f"usable "
                "reference: every strategy would be judged against a baseline that already "
                "fails the "
                "task. Fix the workload or the model before reading a recommendation from this run."
            ),
        )
    eligible = [
        outcome
        for outcome in outcomes
        if outcome.strategy != baseline and relative_correctness(outcome, base) >= min_correctness
    ]
    cheapest_overall = min(
        (o for o in outcomes if o.strategy != baseline), key=lambda o: o.cost, default=base
    )

    if not eligible:
        chosen = base
        rationale = (
            f"No compaction strategy retained {min_correctness:.0%} of the control's correctness. "
            f"The cheapest, {cheapest_overall.strategy!r}, saves "
            f"{(base.cost - cheapest_overall.cost) / base.cost:.0%} but answers at "
            f"{relative_correctness(cheapest_overall, base):.0%} of the control. Compact only "
            f"to avoid "
            "overflowing the context window, not to save money."
        )
    else:
        chosen = min(eligible, key=lambda outcome: outcome.cost)
        if chosen.cost >= base.cost:
            chosen = base
            rationale = (
                "Every strategy that keeps the answer intact also costs more than not compacting. "
                "Leave it off unless the conversation would overflow the window."
            )
        else:
            saving = (base.cost - chosen.cost) / base.cost
            rationale = (
                f"{chosen.strategy!r} is {saving:.0%} cheaper than not compacting while "
                f"answering at "
                f"{relative_correctness(chosen, base):.0%} of the control. "
                f"{len(eligible)} of {len(outcomes) - 1} strategies cleared the correctness bar."
            )

    return JointVerdict(
        recommended=chosen.strategy,
        baseline=base,
        chosen=chosen,
        outcomes=tuple(sorted(outcomes, key=lambda outcome: outcome.cost)),
        min_correctness=min_correctness,
        rationale=rationale,
    )


def _recommend_without_baseline(
    outcomes: list[JointOutcome], base: JointOutcome, min_correctness: float
) -> JointVerdict:
    """Recommend when the control overflowed the window and so is not an option.

    The regime compaction exists for. Not compacting is not a choice a model of this size
    allows, so there is nothing to save against and no "leave it off" to fall back to: the
    answer is the cheapest strategy that kept enough of what the conversation held, or nothing.

    The bar stays anchored on the control's correctness, deliberately. It is the only reading of
    what the whole conversation contained; re-basing it on the best compacting row would judge
    every row against one that may itself have lost facts, and would make the bar move with
    whichever strategies happened to be measured.

    Args:
        outcomes: The admissible strategies, and the control.
        base: The control.
        min_correctness: Fraction of the control's correctness a strategy must retain.

    Returns:
        The verdict, recommending None when no strategy qualifies.
    """
    others = [outcome for outcome in outcomes if outcome.strategy != base.strategy]
    ordered = tuple(sorted(outcomes, key=lambda outcome: outcome.cost))
    overflow = (
        f"The uncompacted control {base.strategy!r} exceeded the context limit, so not "
        f"compacting is not "
        "an option at this size and no row is priced against it. "
    )

    def nothing(reason: str) -> JointVerdict:
        return JointVerdict(
            recommended=None,
            baseline=base,
            chosen=base,
            outcomes=ordered,
            min_correctness=min_correctness,
            rationale=overflow + reason,
            baseline_admissible=False,
        )

    if not others:
        return nothing(
            "No compacting row stayed under the limit either, so nothing can be recommended."
        )
    if base.correctness < MEANINGFUL_BASELINE:
        return nothing(
            f"Its accuracy, {base.correctness:.0%}, is too low to judge what any row kept, so "
            f"nothing "
            "can be recommended. Fix the workload or the model before reading this cell."
        )
    eligible = [
        outcome for outcome in others if relative_correctness(outcome, base) >= min_correctness
    ]
    if not eligible:
        closest = max(others, key=lambda outcome: outcome.correctness)
        return nothing(
            f"No row that stayed under the limit kept {min_correctness:.0%} of the control's "
            f"accuracy, so "
            f"nothing can be recommended. The closest, {closest.strategy!r}, answered at "
            f"{relative_correctness(closest, base):.0%} of it."
        )
    chosen = min(eligible, key=lambda outcome: outcome.cost)
    return JointVerdict(
        recommended=chosen.strategy,
        baseline=base,
        chosen=chosen,
        outcomes=ordered,
        min_correctness=min_correctness,
        rationale=(
            overflow
            + f"{chosen.strategy!r} is the cheapest row that stayed under it while answering at "
            f"{relative_correctness(chosen, base):.0%} of the control's accuracy, measured on "
            f"a prompt no "
            f"model of this size would accept. {len(eligible)} of {len(others)} rows under the "
            f"limit "
            "cleared the bar."
        ),
        baseline_admissible=False,
    )
