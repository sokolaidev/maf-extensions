"""The registry of strategies under test, and the parameters they are all built from.

This is benchmark configuration rather than a strategy of its own: it puts the strategies of
:mod:`maf_compaction` and the framework's behind one name each, so ``--strategies`` selects
between them on equal terms and every row is built from the same :class:`StrategyOptions`.

Each entry wraps a strategy from ``agent_framework`` or from :mod:`maf_compaction` so that the
benchmark can select it by name. ``context_window`` is the strategy the agent harness
installs by default when
``create_harness_agent`` is given ``max_context_window_tokens``; the ``*_aggressive`` and
``*_lazy`` variants are the same strategy at different trigger thresholds and exist to
answer whether compacting early and often costs more in lost cache reads than it saves in
prompt tokens.

The ``token_budget_*`` family is different in kind from the rest. Every other entry decides
*when* to compact from its own trigger, so different strategies leave prompts of different
sizes and a comparison between them confounds "trimmed harder" with "trimmed smarter". The
composed variants all compact down to one shared ceiling and differ only in the order they
delete things, which holds size fixed and isolates the choice of what to discard.
``token_budget_fallback`` composes nothing at all, so its removals are pure oldest-first
eviction: the floor any ordering has to beat to be worth its complexity.

``tool_and_user_summary_anchored`` is a composition of a third kind, and deliberately not a
member of that family. It runs ``tool_summary_anchored`` and then ``user_summary_anchored``
over one conversation and the two do *not* meet at a shared ceiling: what the row is for is how
far the two halves reach together, and normalising their sizes away is exactly what the
``token_budget_*`` family does. Its parts are built by the same two builders the single rows
use, so a sweep of any of their flags moves this row's half the way it moves theirs.

What it does share is one *trigger*. The two halves are judged at ``--trigger-fraction``: the
record half against the prompt the pass began with, the user half against the prompt the record
half left, so the user half -- whose edits break the cached prefix -- acts only when tool
compaction was not enough. Behind both runs a last-resort chain, merging records, merging user
summaries and rewriting the record harder before the record half's fallback, and only while the
prompt is over the input budget. ``maf_compaction._composed`` carries the argument, including
why the user half is judged after the record phase and why the alignment runs down to the record
half's line rather than up to the user half's.

Budgets are sized relative to the transcript rather than to a model's real context window.
A 20-turn transcript never approaches a 128k window, so a real window would mean no
strategy ever fires and the benchmark would measure nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from agent_framework import (
    CompactionStrategy,
    ContextWindowCompactionStrategy,
    SelectiveToolCallCompactionStrategy,
    SlidingWindowStrategy,
    SummarizationStrategy,
    TokenBudgetComposedStrategy,
    TokenizerProtocol,
    ToolResultCompactionStrategy,
    TruncationStrategy,
)
from maf_compaction import (
    DEFAULT_BAND_SHARE,
    DEFAULT_CHAIN_GAIN_FRACTION,
    DEFAULT_COVERAGE_SHARE,
    DEFAULT_FALLBACK_FRACTION,
    DEFAULT_HARDER_ATTEMPTS,
    DEFAULT_KEEP_HEAD_USER_TURNS,
    DEFAULT_KEEP_TAIL_USER_TURNS,
    DEFAULT_MIN_BAND_SHARE,
    DEFAULT_MIN_GAIN_FRACTION,
    DEFAULT_SUMMARY_MODE,
    DEFAULT_TRIGGER_FRACTION,
    DEFAULT_USER_TRIGGER_FRACTION,
    SUMMARY_MODE_BOUNDARY,
    AnchoredCompactionStrategy,
    MinimumGainAnchoredCompactionStrategy,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    UserTurnAnchoredSummarizationCompactionStrategy,
)

if TYPE_CHECKING:
    from agent_framework._clients import SupportsChatGetResponse

__all__ = [
    "STRATEGY_BUILDERS",
    "StrategyOptions",
    "build_strategy",
    "resolve_context_window",
    "strategy_names",
]

# Fraction of a transcript's fully-replayed prompt size used as the simulated context
# window when the caller does not pass one. Below 1.0 so that compaction is guaranteed to
# trigger part-way through every preset.
_AUTO_WINDOW_FRACTION: Final[float] = 0.6
# The floor has to leave the system anchor inside even the most aggressive phase's budget.
# ContextWindowCompactionStrategy evicts tool results at 0.5 of the input budget, so a
# window that puts half the budget below the anchor's size drives the composed budget
# strategy's strict pass -- `token_budget_fallback_strict`, which is where the framework's
# only strict eviction lives -- into evicting the anchor itself. Measured on the `small`
# preset at a 2,048 floor, where the prompt collapsed to 47 tokens on turn 1. That destroys
# the stable prefix whose cacheability is the entire subject of the benchmark.
_MIN_AUTO_WINDOW_TOKENS: Final[int] = 4_096


@dataclass(frozen=True, slots=True)
class StrategyOptions:
    """CLI-settable parameters shared by the strategy builders.

    Strategy constructors validate their ranges during preflight.
    """

    tokenizer: TokenizerProtocol
    max_context_window_tokens: int
    max_output_tokens: int
    keep_last_groups: int = 6
    keep_last_tool_call_groups: int = 4
    keep_head_groups: int = 3
    keep_tail_groups: int = 4
    keep_tokens: int | None = None
    """Tokens of a collapsed tool result the anchored family retains, head and tail together.

    ``None`` derives it from ``band_share`` and the result's position in the band, which is
    what makes retention scale with the window instead of shrinking to nothing as results grow.
    A number fixes it, which is the older behaviour and is worth being able to reproduce: the
    two answer different questions about the same row.
    """
    band_share: float = DEFAULT_BAND_SHARE
    min_gain_fraction: float = DEFAULT_MIN_GAIN_FRACTION
    """Break-even floor under every collapse made by ``anchored_min_gain``."""
    trigger_fraction: float = DEFAULT_TRIGGER_FRACTION
    """Share of the input budget at which ``tool_summary_anchored`` asks for its record.

    Reaches both halves of that strategy from here: the run hands it to the strategy, and the
    middleware takes the strategy's own value rather than a second copy, so the ask and the
    wait cannot be configured apart.
    """
    fallback_fraction: float = DEFAULT_FALLBACK_FRACTION
    """Share at which ``tool_summary_anchored`` stops waiting and compacts without a record.

    Must exceed ``trigger_fraction``; the strategy raises ``ValueError`` when it does not.
    """
    coverage_share: float = DEFAULT_COVERAGE_SHARE
    """Share of a group's distinctive values a record must quote before the group may be cut.

    The dial on the coverage check, whose default is a threshold rather than a derivation and
    whose right value depends on how many values a workload's results carry.
    """
    keep_head_user_turns: int = DEFAULT_KEEP_HEAD_USER_TURNS
    """User turns ``user_summary_anchored`` never summarises at the start of the conversation.

    Counted in user turns rather than in message groups, which is why it is not
    ``keep_head_groups``: that number protects a prefix of every kind of group and is read by
    four strategies, and pointing this at it would make a change intended for the tool band
    silently move which user turns survive.
    """
    keep_tail_user_turns: int = DEFAULT_KEEP_TAIL_USER_TURNS
    """User turns ``user_summary_anchored`` never summarises at the end of the conversation.

    One by default because the last user turn is the live request. Separate from
    ``keep_head_user_turns`` so the two ends can be moved apart, which is the only way to
    measure what the opening task statement is worth against what the recent turns are worth.
    """
    user_trigger_fraction: float = DEFAULT_USER_TRIGGER_FRACTION
    """Share of the input budget at which ``user_summary_anchored`` summarises the user band.

    Its own field rather than a second reading of ``trigger_fraction``, which belongs to
    ``tool_summary_anchored``. The two defaults differ -- 0.8 here against 0.6 there -- because
    the decisions are different: one asks a model for a record and must ask before the bulk
    degrades it, this one pays only in a broken cached prefix and wants to fire as late as it
    still can. Sharing a field would have made a sweep of either one a sweep of both.
    """
    user_min_band_share: float = DEFAULT_MIN_BAND_SHARE
    """Share of the prompt the user band must be worth before ``user_summary_anchored`` acts.

    The hysteresis, and the field that says what a row of that strategy means. Without it the
    trigger alone fires the strategy once per turn for the rest of a run that stays above it --
    30 passes in a measured run where the design expects one or two -- because the band it reads
    after its first pass is its own summary plus the turns since. Sweepable because the right
    value is a property of the workload's user share rather than of the strategy: 0.0 is the
    behaviour every run before this measured, and every archived row is one.
    """
    user_summary_mode: str = DEFAULT_SUMMARY_MODE
    """What ``user_summary_anchored`` does with the summary its previous pass left behind.

    ``recompact``, ``boundary`` or ``fold``; ``compaction/_usersummary`` says what each buys and
    what each costs. Sweepable because the three are the arms of one measurement, and the default
    is the arm the archive was taken on -- the recompacting one -- until a run has measured the
    others against it.

    This affects only the standalone row; the composed row always uses ``boundary``.
    """
    record_harder_attempts: int = DEFAULT_HARDER_ATTEMPTS
    """Harder rewrites of the record ``tool_and_user_summary_anchored`` may try per pass when over.

    Step c of that row's last-resort chain, and read by no other row. ``compaction/_composed``
    says why the default is two; zero switches the step off.
    """
    chain_gain_fraction: float = DEFAULT_CHAIN_GAIN_FRACTION
    """Share of the tokens behind its earliest edit the composed strategy's chain removes.

    The chain's hysteresis, read by that row alone: once the prompt is over the input budget
    the chain goes on, past the budget, until a firing has removed this share of what stands
    behind the earliest edit it made, so the next turns fit without another early edit. The
    default is the break-even share an edit must remove to repay the re-bill it causes, the
    anchored floor's number; ``compaction/_composed`` carries the argument. Zero stops the chain
    at the budget, as it stopped before the setting existed.
    """
    token_budget_fraction: float = 0.5
    summarizer: SupportsChatGetResponse[Any] | None = None

    @property
    def input_budget_tokens(self) -> int:
        """Tokens available for input once the output reservation is deducted."""
        return self.max_context_window_tokens - self.max_output_tokens

    @property
    def composed_budget_tokens(self) -> int:
        """Token ceiling every ``token_budget_*`` variant compacts down to.

        Shared across the variants on purpose. They differ only in the order they delete
        things, so holding the ceiling fixed is what makes their correctness scores
        comparable: any difference is attributable to *what* each discarded, not how much.
        """
        return max(int(self.input_budget_tokens * self.token_budget_fraction), 1)


def resolve_context_window(
    transcript_tokens: int,
    *,
    override: int | None = None,
    max_output_tokens: int = 512,
) -> int:
    """Return the simulated context window to compact against.

    Args:
        transcript_tokens: Approximate prompt size of the fully replayed transcript.

    Keyword Args:
        override: Explicit window size. When given, it is used verbatim.
        max_output_tokens: Output reservation, used only to enforce a sane lower bound.

    Returns:
        A window size that guarantees compaction triggers part-way through the transcript.
    """
    if override is not None:
        return override
    scaled = int(transcript_tokens * _AUTO_WINDOW_FRACTION)
    return max(scaled, _MIN_AUTO_WINDOW_TOKENS, max_output_tokens * 2)


def _build_none(options: StrategyOptions) -> CompactionStrategy | None:
    """Return no strategy, establishing the uncompacted baseline."""
    return None


def _context_window(
    options: StrategyOptions, *, eviction: float, truncation: float
) -> CompactionStrategy:
    """Return the harness default strategy at explicit trigger thresholds."""
    return ContextWindowCompactionStrategy(
        max_context_window_tokens=options.max_context_window_tokens,
        max_output_tokens=options.max_output_tokens,
        tokenizer=options.tokenizer,
        tool_eviction_threshold=eviction,
        truncation_threshold=truncation,
        keep_last_tool_call_groups=options.keep_last_tool_call_groups,
    )


def _build_context_window(options: StrategyOptions) -> CompactionStrategy:
    """Return the harness default: shipped thresholds of 0.5 and 0.8.

    This row is meant to stand for what ``create_harness_agent`` actually installs, so its
    ``keep_last_tool_call_groups`` has to match the framework's default of 4 rather than
    being set locally. The harness passes no value, so it inherits that default; a lab
    override would quietly make this row harsher than the configuration it claims to
    represent, and every conclusion drawn about "the shipped default" would be about
    something else.
    """
    return _context_window(options, eviction=0.5, truncation=0.8)


def _build_context_window_aggressive(options: StrategyOptions) -> CompactionStrategy:
    """Return the harness default compacting early, at 0.3 and 0.5 of the input budget."""
    return _context_window(options, eviction=0.3, truncation=0.5)


def _build_context_window_lazy(options: StrategyOptions) -> CompactionStrategy:
    """Return the harness default compacting late, at 0.7 and 0.95 of the input budget."""
    return _context_window(options, eviction=0.7, truncation=0.95)


def _build_anchored(options: StrategyOptions) -> AnchoredCompactionStrategy:
    """Return the anchored strategy, designed against what the framework's rows measured.

    Its ceiling is the full input budget rather than a fraction of it, because unlike the
    threshold-driven strategies it does not need headroom to trip: it collapses the middle
    band from the first turn there is one, and only removes groups outright when shortening
    has not brought the prompt under. See :mod:`maf_compaction._anchored`.
    """
    return AnchoredCompactionStrategy(
        max_input_tokens=options.input_budget_tokens,
        tokenizer=options.tokenizer,
        keep_head_groups=options.keep_head_groups,
        keep_tail_groups=options.keep_tail_groups,
        keep_tokens=options.keep_tokens,
        band_share=options.band_share,
    )


def _build_anchored_no_assistant(options: StrategyOptions) -> CompactionStrategy:
    """Return the anchored strategy forbidden from touching assistant narration.

    Pairs with ``anchored`` to isolate the last-resort step. With the harness's default
    instructions the model restates tool values in its prose, so that prose can be the only
    surviving copy of a result that has already been shortened; this row measures what
    dropping it costs.
    """
    return AnchoredCompactionStrategy(
        max_input_tokens=options.input_budget_tokens,
        tokenizer=options.tokenizer,
        keep_head_groups=options.keep_head_groups,
        keep_tail_groups=options.keep_tail_groups,
        keep_tokens=options.keep_tokens,
        band_share=options.band_share,
        collapse_assistant_text=False,
    )


def _build_anchored_min_gain(options: StrategyOptions) -> CompactionStrategy:
    """Return the anchored strategy with a break-even floor under every collapse.

    Pairs with ``anchored`` to measure one setting: whether declining collapses too small to
    repay the prompt cache they invalidate is worth the information they would have removed.
    """
    return MinimumGainAnchoredCompactionStrategy(
        max_input_tokens=options.input_budget_tokens,
        tokenizer=options.tokenizer,
        keep_head_groups=options.keep_head_groups,
        keep_tail_groups=options.keep_tail_groups,
        keep_tokens=options.keep_tokens,
        band_share=options.band_share,
        min_gain_fraction=options.min_gain_fraction,
    )


def _build_tool_summary_anchored(
    options: StrategyOptions,
) -> ToolResultAnchoredSummarizationCompactionStrategy:
    """Return the record-then-drop strategy.

    Needs no summarizer client of its own: the recording is done by the agent's own model
    through a tool call the provider issues. That is also why a run using it cannot pin
    ``tool_choice`` -- the model has to be free to choose the recall tool.

    The fallback is built here rather than left to the strategy's own default. The default is
    the same object with the same head and tail, but it takes the anchored strategy's *own*
    defaults for ``band_share`` and ``keep_tokens``, so a sweep moving either of those moved
    every anchored row except the one hiding inside this one -- and this row falls back often
    enough that the difference is measured rather than theoretical.
    """
    return ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=options.input_budget_tokens,
        tokenizer=options.tokenizer,
        keep_head_groups=options.keep_head_groups,
        keep_tail_groups=options.keep_tail_groups,
        trigger_fraction=options.trigger_fraction,
        fallback_fraction=options.fallback_fraction,
        coverage_share=options.coverage_share,
        fallback=_build_anchored(options),
    )


def _build_user_summary_anchored(
    options: StrategyOptions, *, remembered_requests: int = 1
) -> UserTurnAnchoredSummarizationCompactionStrategy:
    """Return the strategy that compacts the user's own turns.

    The mirror of ``tool_summary_anchored`` on the other half of the conversation, and the pair
    is the reason both exist: each touches only its own half, so the two rows measure what a
    strategy can remove from the user side and from the tool side independently rather than
    reporting one number for both. On a fixed-payload sizing that is not a small difference:
    a seeded conversation can be 57% user-turn text against 14% tool results.

    Its ceiling is the full input budget, like the anchored family's and unlike the
    threshold-driven framework strategies: the trigger is a fraction of that ceiling and is
    configured separately, so subtracting a second margin here would make the flag mean
    something other than what it says.

    ``remembered_requests`` is the composed row's, and the single row leaves it at one: see the
    user-turn strategy's constructor.

    Raises:
        ValueError: If no summarizer client was configured.
    """
    if options.summarizer is None:
        raise ValueError(
            "The 'user_summary_anchored' strategy needs a summarizer client. "
            "Pass --summarizer-provider to select one, or drop this strategy from the run."
        )
    return UserTurnAnchoredSummarizationCompactionStrategy(
        max_input_tokens=options.input_budget_tokens,
        tokenizer=options.tokenizer,
        client=options.summarizer,
        keep_head_user_turns=options.keep_head_user_turns,
        keep_tail_user_turns=options.keep_tail_user_turns,
        trigger_fraction=options.user_trigger_fraction,
        min_band_share=options.user_min_band_share,
        summary_mode=options.user_summary_mode,
        remembered_requests=remembered_requests,
    )


def _build_tool_and_user_summary_anchored(options: StrategyOptions) -> CompactionStrategy:
    """Return both summarising strategies over one conversation, the record one first.

    Built from the same two builders the single rows use rather than from two fresh
    constructor calls, which is what makes the comparison the row exists for legitimate: a
    sweep moving ``--coverage-share`` or ``--keep-head-user-turns`` moves this row's half in
    exactly the way it moves the corresponding single row, and neither half can drift into a
    configuration no other row was measured at.

    Three settings do not reach this row, and each is this row's own configuration rather than a new
    default for the objects. ``--user-trigger-fraction``: the composition judges both halves at
    ``--trigger-fraction``, taking that line from the record half it was handed, so a sweep of the
    user row's trigger moves the single row only. ``--user-summary-mode``: this row's user half runs
    in ``boundary``, so its summaries stand rather than being re-summarised, and it remembers two
    summarizer requests rather than one, because its last-resort chain may ask for a fold on the
    pass that summarised a band. ``--record-repeats``: the recall middleware asks again for every
    new batch of tool work on this row even under ``--no-record-repeats`` -- the composed object
    reports ``repeat_records`` and ``run_live`` reads it. ``compaction/_composed`` carries the
    argument for all three.

    Selecting this is selecting both halves' costs together. The record half spends an agent
    turn writing its record and the user half spends a summarizer call, so a cell running this
    beside ``none`` is comparing a row with two extra call types against a row with none.

    Raises:
        ValueError: If no summarizer client was configured, from the user-band half. The
            record half needs none: its record is written by the agent's own model.
    """
    return ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=options.tokenizer,
        tool_results=_build_tool_summary_anchored(options),
        user_turns=_build_user_summary_anchored(
            replace(options, user_summary_mode=SUMMARY_MODE_BOUNDARY), remembered_requests=2
        ),
        harder_attempts=options.record_harder_attempts,
        chain_gain_fraction=options.chain_gain_fraction,
    )


def _build_truncation(options: StrategyOptions) -> CompactionStrategy:
    """Return oldest-first truncation triggering at 80% of the input budget."""
    budget = options.input_budget_tokens
    return TruncationStrategy(
        max_n=max(int(budget * 0.8), 1),
        compact_to=max(int(budget * 0.5), 1),
        tokenizer=options.tokenizer,
    )


def _build_sliding_window(options: StrategyOptions) -> CompactionStrategy:
    """Return a fixed-size window over the most recent message groups."""
    return SlidingWindowStrategy(keep_last_groups=options.keep_last_groups)


def _build_tool_result(options: StrategyOptions) -> CompactionStrategy:
    """Return tool-result eviction, which rewrites history in place instead of dropping it."""
    return ToolResultCompactionStrategy(
        keep_last_tool_call_groups=options.keep_last_tool_call_groups
    )


def _build_selective_tool_call(options: StrategyOptions) -> CompactionStrategy:
    """Return selective removal of older tool-call groups."""
    return SelectiveToolCallCompactionStrategy(
        keep_last_tool_call_groups=options.keep_last_tool_call_groups
    )


def _build_summarization(options: StrategyOptions) -> CompactionStrategy:
    """Return LLM summarization of older turns.

    The summarizer's input is bounded by the cell's own input budget rather than the
    framework's default of 8,000 tokens. The framework selects whole message groups until the
    next one would exceed the bound and skips any single group that cannot fit, and its
    transcript carries each tool result in full. Under the default a benchmark cell whose tool
    results run to tens of thousands of tokens therefore never has them summarized: the strategy
    spends a summarizer call per turn on the small groups around them and the prompt never
    leaves the window. The summarizer is the model under test, so its window is the bound that
    applies.

    Raises:
        ValueError: If no summarizer client was configured.
    """
    if options.summarizer is None:
        raise ValueError(
            "The 'summarization' strategy needs a summarizer client. "
            "Pass --summarizer-provider to select one, or drop this strategy from the run."
        )
    return SummarizationStrategy(
        client=options.summarizer,
        target_count=options.keep_last_groups,
        max_summary_input_tokens=options.input_budget_tokens,
        tokenizer=options.tokenizer,
    )


def _truncation_at(options: StrategyOptions, budget: int) -> CompactionStrategy:
    """Return truncation targeting a composed strategy's budget."""
    return TruncationStrategy(
        max_n=budget, compact_to=max(int(budget * 0.8), 1), tokenizer=options.tokenizer
    )


def _composed(options: StrategyOptions, parts: list[CompactionStrategy]) -> CompactionStrategy:
    """Return an ordered composition run against the shared token ceiling.

    ``TokenBudgetComposedStrategy`` runs each part in turn, re-counting tokens after every
    one and stopping as soon as the ceiling is met. Whatever the parts fail to remove, its
    built-in fallback removes by evicting oldest groups. That fallback is why every variant
    lands at the same size, and why the interesting difference between them is which
    messages they chose to spend the budget on.
    """
    return TokenBudgetComposedStrategy(
        token_budget=options.composed_budget_tokens,
        tokenizer=options.tokenizer,
        strategies=parts,
    )


def _build_token_budget_fallback(options: StrategyOptions) -> CompactionStrategy:
    """Return the composed strategy with no parts at all.

    The control for the whole ``token_budget_*`` family: every removal is done by the
    built-in oldest-first fallback. A variant that cannot beat this is contributing
    nothing over plain age-ordered eviction at the same size.
    """
    return _composed(options, [])


def _build_token_budget_tools_first(options: StrategyOptions) -> CompactionStrategy:
    """Return a composition that sheds tool bulk before it sheds history."""
    return _composed(
        options,
        [
            ToolResultCompactionStrategy(
                keep_last_tool_call_groups=options.keep_last_tool_call_groups
            ),
            SelectiveToolCallCompactionStrategy(
                keep_last_tool_call_groups=options.keep_last_tool_call_groups
            ),
            _truncation_at(options, options.composed_budget_tokens),
        ],
    )


def _build_token_budget_truncate_first(options: StrategyOptions) -> CompactionStrategy:
    """Return a composition that sheds age before it sheds tool bulk.

    The mirror of ``token_budget_tools_first``. Same parts, opposite order, same ceiling,
    so the pair isolates whether ordering alone changes what survives.
    """
    return _composed(
        options,
        [
            _truncation_at(options, options.composed_budget_tokens),
            ToolResultCompactionStrategy(
                keep_last_tool_call_groups=options.keep_last_tool_call_groups
            ),
        ],
    )


def _build_token_budget_window_first(options: StrategyOptions) -> CompactionStrategy:
    """Return a composition that applies a hard recency window before trimming by tokens."""
    return _composed(
        options,
        [
            SlidingWindowStrategy(keep_last_groups=options.keep_last_groups),
            _truncation_at(options, options.composed_budget_tokens),
        ],
    )


def _build_token_budget_summarize(options: StrategyOptions) -> CompactionStrategy:
    """Return a composition that summarizes rather than deletes once tool bulk is gone.

    The only variant that can carry information past the ceiling instead of dropping it,
    and the only one that spends money to do so.

    Raises:
        ValueError: If no summarizer client was configured.
    """
    if options.summarizer is None:
        raise ValueError(
            "The 'token_budget_summarize' strategy needs a summarizer client. "
            "Pass --summarizer-provider to select one, or drop this strategy from the run."
        )
    return _composed(
        options,
        [
            ToolResultCompactionStrategy(
                keep_last_tool_call_groups=options.keep_last_tool_call_groups
            ),
            SummarizationStrategy(
                client=options.summarizer,
                target_count=options.keep_last_groups,
                max_summary_input_tokens=options.input_budget_tokens,
                tokenizer=options.tokenizer,
            ),
        ],
    )


STRATEGY_BUILDERS: Final[dict[str, Callable[[StrategyOptions], CompactionStrategy | None]]] = {
    "none": _build_none,
    "context_window": _build_context_window,
    "context_window_aggressive": _build_context_window_aggressive,
    "context_window_lazy": _build_context_window_lazy,
    "truncation": _build_truncation,
    "anchored": _build_anchored,
    "tool_summary_anchored": _build_tool_summary_anchored,
    "user_summary_anchored": _build_user_summary_anchored,
    "tool_and_user_summary_anchored": _build_tool_and_user_summary_anchored,
    "anchored_no_assistant": _build_anchored_no_assistant,
    "anchored_min_gain": _build_anchored_min_gain,
    "sliding_window": _build_sliding_window,
    "tool_result": _build_tool_result,
    "selective_tool_call": _build_selective_tool_call,
    "summarization": _build_summarization,
    "token_budget_fallback": _build_token_budget_fallback,
    "token_budget_tools_first": _build_token_budget_tools_first,
    "token_budget_truncate_first": _build_token_budget_truncate_first,
    "token_budget_window_first": _build_token_budget_window_first,
    "token_budget_summarize": _build_token_budget_summarize,
}


#: Strategies that cannot be built without a summarizer client. Named explicitly rather than
#: detected by looking for "summar" in the name: that convention silently required a client
#: for a strategy that does its recording through the agent's own tool loop, and would just as
#: silently fail to require one for a summarizing strategy named otherwise.
#:
#: ``user_summary_anchored`` is here and ``tool_summary_anchored`` is not, which is the whole
#: point of naming them: the two read as a pair but only one of them calls a summarizer. The
#: other has the agent's own model write its record through a tool call, so it needs no client
#: at all, and a rule matching on "summary" would have demanded one from it.
#:
#: ``tool_and_user_summary_anchored`` is here because it *contains* the one that needs a
#: client, and that is the cost of naming rather than deriving: a composed strategy's needs
#: are its parts' needs unioned, and nothing computes that union for a set written out by
#: hand. Both readers of this set break when a composed name is left out of it, and neither
#: breaks loudly. The run's pre-flight would not demand ``--summarizer-provider`` for a cell
#: that cannot run without one, and ``_build_or_exit`` -- which skips the names in this set
#: precisely so a missing client is not reported as a bad value -- would build the row with
#: ``summarizer=None`` and exit saying the *configuration* was rejected, about a flag that
#: was simply not passed.
STRATEGIES_NEEDING_SUMMARIZER: Final[frozenset[str]] = frozenset(
    {
        "summarization",
        "token_budget_summarize",
        "tool_and_user_summary_anchored",
        "user_summary_anchored",
    }
)


#: Strategies whose run installs the recall middleware, and so forces a recall call.
#:
#: Named for the reason :data:`STRATEGIES_NEEDING_SUMMARIZER` is named, and it is the same
#: hazard one category along: ``--record-max-tokens`` and ``--record-target-tokens`` are
#: settings of a call that only these rows make, and the warning about the first being above
#: the run's output reservation is printed from a name test. A composed row that runs the
#: record strategy as a phase makes that call and inherits that hazard, so leaving it out of
#: this set would silence a warning about a configuration the run is genuinely in.
#:
#: What actually installs the middleware is ``_live.run_live``, which finds the record
#: strategy inside whatever was built rather than matching a name -- see
#: ``find_nested_strategy``. This set exists only for the checks that run before anything is
#: built, and a name added to :data:`STRATEGY_BUILDERS` that composes the record strategy
#: belongs in both places.
STRATEGIES_FORCING_RECORDS: Final[frozenset[str]] = frozenset(
    {
        "tool_and_user_summary_anchored",
        "tool_summary_anchored",
    }
)


def forces_records(names: Iterable[str]) -> bool:
    """Return whether any of ``names`` has the middleware force a recall call.

    Args:
        names: Strategy names selected for a run.

    Returns:
        True when at least one writes a record.
    """
    return any(name in STRATEGIES_FORCING_RECORDS for name in names)


def needs_summarizer(names: Iterable[str]) -> bool:
    """Return whether any of ``names`` requires a summarizer client.

    Args:
        names: Strategy names selected for a run.

    Returns:
        True when at least one needs a client.
    """
    return any(name in STRATEGIES_NEEDING_SUMMARIZER for name in names)


def strategy_names() -> list[str]:
    """Return every selectable strategy name."""
    return list(STRATEGY_BUILDERS)


def build_strategy(name: str, options: StrategyOptions) -> CompactionStrategy | None:
    """Build the named strategy.

    Args:
        name: One of the keys of ``STRATEGY_BUILDERS``.
        options: Shared budget and tokenizer parameters.

    Returns:
        The strategy, or ``None`` for the uncompacted ``none`` baseline.

    Raises:
        KeyError: If ``name`` is not a known strategy.
    """
    if name not in STRATEGY_BUILDERS:
        raise KeyError(f"Unknown strategy {name!r}. Known strategies: {sorted(STRATEGY_BUILDERS)}")
    return STRATEGY_BUILDERS[name](options)
