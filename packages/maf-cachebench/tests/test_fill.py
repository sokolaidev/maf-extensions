"""Unit tests for the analytic fill sizing.

The sizing is a claim about a conversation that has not been run yet, so it is checked here
against a stub that bills what it was actually sent. That closes the loop offline: the plan
counts the material the generator is about to produce, the stub counts the material the agent
actually sent, and the two are independent walks over the same conversation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from agent_framework import CharacterEstimatorTokenizer, Message, UsageDetails
from test_live import StubChatClient

from maf_cachebench import (
    FillPlan,
    ProviderRuntime,
    StrategyOptions,
    build_live_scenario,
    plan_fill,
    run_live,
)
from maf_cachebench._fill import _FUNCTION_CALL_TOKENS
from maf_cachebench._live_cli import FILL_TOLERANCE
from maf_cachebench._transcripts import filler_text

pytestmark = pytest.mark.anyio

TOKENIZER = CharacterEstimatorTokenizer()

#: A reply large enough to matter in the arithmetic. The replies are the one term the sizing
#: cannot compute, so a stub that answered in three words would let a plan that ignored them
#: pass.
REPLY = filler_text(11, 600)
REPLY_TOKENS = TOKENIZER.count_tokens(REPLY)


class SizingStubClient(StubChatClient):
    """A stub that bills what it was sent, counted the way the plan counts it.

    Not the same code as the plan: this walks the messages the agent actually assembled, while
    the plan walks the scenario it is about to generate. A sizing error shows up as a
    disagreement between the two.

    ``sizes`` keeps the tool half of each prompt beside its total, which is what makes the tool
    share checkable end to end. The billed usage cannot carry it -- nothing on the wire
    separates a tool result from the turn around it -- so it is recorded here as the prompt is
    walked and matched back to a call by its total.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sizes: list[tuple[int, int]] = []

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        total = TOKENIZER.count_tokens(str(options.get("instructions") or ""))
        tool_total = 0
        for message in messages:
            for content in message.contents:
                if content.type == "function_call":
                    tool_total += _FUNCTION_CALL_TOKENS
                elif content.type == "function_result":
                    tool_total += TOKENIZER.count_tokens(str(content.result))
                elif text := getattr(content, "text", None):
                    total += TOKENIZER.count_tokens(text)
        total += tool_total
        self.sizes.append((total, tool_total))
        # Set before the response is built, and read when it is awaited a moment later. Calls
        # are strictly sequential here, so there is no window in which the two disagree.
        self.usage = UsageDetails(input_token_count=total, output_token_count=REPLY_TOKENS)
        return super()._inner_get_response(
            messages=messages, stream=stream, options=options, **kwargs
        )


def _plan(fill: float, *, context_limit: int = 40_000, **kwargs: Any) -> FillPlan:
    settings: dict[str, Any] = {
        "tool_turns": 6,
        "tool_result_tokens": 500,
        "filler_turn_tokens": 800,
        "reply_tokens": REPLY_TOKENS,
        **kwargs,
    }
    return plan_fill(
        tokenizer=TOKENIZER, context_limit=context_limit, fill_fraction=fill, **settings
    )


def test_the_plan_predicts_the_target_it_was_given() -> None:
    """The arithmetic must close before anything is run.

    A plan that misses here misses by the same amount on every strategy, so the cell is
    labelled with a fill fraction it never reached and sits on a different axis from its
    neighbours without anything saying so.
    """
    plan = _plan(0.5)

    assert plan.target_tokens == 20_000
    assert abs(plan.deviation) < 0.01


async def test_the_seeded_conversation_lands_on_the_target() -> None:
    """The conversation an uncompacted run actually sends must reach the planned size.

    This is the claim the fill fraction rests on, and it is checked end to end rather than
    against the plan's own arithmetic: the stub counts the assembled prompt, which is a
    different walk over the conversation than the one that sized it.
    """
    plan = _plan(0.5)
    scenario = build_live_scenario(
        salt="fill",
        filler_turns=plan.filler_turns,
        filler_tokens=plan.filler_tokens,
        tool_turns=6,
        narration="neutral",
    )
    outcome = await run_live(
        ProviderRuntime(client=SizingStubClient(reply=REPLY, obey_tool_choice=True), model="stub"),
        strategy_name="none",
        options=StrategyOptions(TOKENIZER, 40_000, 2_048),
        scenario=scenario,
        tool_result_tokens=500,
        narration="neutral",
        probe_repeats=1,
    )

    deviation = (outcome.seed_prompt_tokens - plan.target_tokens) / plan.target_tokens
    assert abs(deviation) <= FILL_TOLERANCE, (
        f"seeded {outcome.seed_prompt_tokens:,} against {plan.target_tokens:,}"
    )


def test_filler_is_the_dial_and_the_payload_is_fixed() -> None:
    """Raising the fill must add filler and leave the planted material alone.

    The fill fraction only means "how much irrelevant context surrounds a fixed set of facts"
    if the facts and their carriers are the same in every cell. A sizing that reached its
    target by enlarging the tool results would be measuring two things at once and reporting
    one number.
    """
    low, high = _plan(0.5), _plan(0.8)

    assert high.filler_turns * high.filler_tokens > low.filler_turns * low.filler_tokens
    assert high.payload_tokens == low.payload_tokens
    assert high.tool_payload_tokens == low.tool_payload_tokens


def test_a_payload_larger_than_the_smallest_cell_is_rejected() -> None:
    """A payload that does not fit cannot be filled around, and must say so.

    Silently building the cell anyway produces a conversation that overshoots its own target
    before a single filler turn is added, which reads in the table as a strategy failing to
    compact rather than as a matrix that cannot be built.
    """
    with pytest.raises(ValueError) as error:
        plan_fill(
            tokenizer=TOKENIZER,
            context_limit=10_000,
            fill_fraction=0.5,
            tool_turns=8,
            tool_result_tokens=4_000,
            reply_tokens=REPLY_TOKENS,
        )

    message = str(error.value)
    assert "payload does not fit" in message
    assert "--tool-result-tokens" in message, "the error must name what to change"
    assert "5,000" in message, "the error must state the target it did not fit inside"


def test_the_plan_leaves_room_for_every_tool_group_it_planted() -> None:
    """Extra lookups live inside the filler sections, so too little filler drops them.

    Measured before this was enforced: 16 tool turns asked for with the default 6 filler turns
    yielded 9, and the payload was quietly a different payload from the one the run was
    labelled with.
    """
    plan = plan_fill(
        tokenizer=TOKENIZER,
        context_limit=200_000,
        fill_fraction=0.5,
        tool_turns=16,
        filler_tool_turns=4,
        tool_result_tokens=500,
        reply_tokens=REPLY_TOKENS,
    )
    scenario = build_live_scenario(
        salt="groups",
        filler_turns=plan.filler_turns,
        filler_tokens=plan.filler_tokens,
        tool_turns=16,
    )

    assert len(scenario.tool_lookups) == 16


@pytest.mark.parametrize("fill", [0.0, -0.5, 10.5, 65.0])
def test_an_impossible_fill_fraction_is_refused(fill: float) -> None:
    """A fraction outside (0, 10] describes no cell, and would silently size to nonsense.

    65 is the typo the cap is kept for: a percentage written where a fraction was meant.
    """
    with pytest.raises(ValueError, match="fill_fraction"):
        _plan(fill)


def test_a_fill_past_the_window_sizes_the_control_to_overflow_it() -> None:
    """Above 1.0 is the cell compaction exists for, and the solve does not change for it.

    The cap at 1.0 had no stated reason and made the one regime every archived verdict was
    missing unreachable: at a capped 1.0 a luna control, whose replies run shorter than assumed,
    seeded around 113,000 tokens against a 120,000 limit and never disqualified.
    """
    plan = _plan(1.15)

    assert plan.target_tokens == round(40_000 * 1.15)
    assert plan.predicted_tokens > plan.context_limit
    assert abs(plan.deviation) <= FILL_TOLERANCE


@pytest.mark.parametrize("context_limit", [40_000, 120_000, 272_000])
@pytest.mark.parametrize("fill", [0.5, 0.86])
@pytest.mark.parametrize("share", [0.25, 0.6, 0.8])
def test_the_derived_result_size_lands_on_the_requested_share(
    context_limit: int, fill: float, share: float
) -> None:
    """The point of the share is that it holds across windows and fills, so it is checked across both.

    An absolute result size does not: 3,500 tokens is 6% of a 60,000-token context and 3% of a
    120,000-token one, so a sweep over window sizes is a sweep over two variables. The share is
    only the fix for that if the solver actually reaches it wherever it is asked.
    """
    plan = plan_fill(
        tokenizer=TOKENIZER,
        context_limit=context_limit,
        fill_fraction=fill,
        tool_turns=6,
        markers_per_tool=8,
        tool_share=share,
        filler_turn_tokens=800,
        reply_tokens=REPLY_TOKENS,
    )

    assert abs(plan.tool_share_deviation) <= FILL_TOLERANCE, (
        f"{plan.tool_payload_tokens:,} of {plan.predicted_tokens:,} is "
        f"{plan.achieved_tool_share:.1%} against {share:.0%} asked for"
    )
    assert plan.tool_result_tokens > 0


def test_a_larger_window_derives_a_larger_result_at_the_same_share() -> None:
    """This is the failure the parameter exists for, stated as an assertion.

    ``AnchoredCompactionStrategy`` shortens each banded result to a share of the *ceiling*, so
    a payload held at an absolute size stops being large enough to shorten as the window grows:
    at 3,500-token results its allowance was about 2,900 tokens at 60,000 and about 5,900 at
    120,000, where it planned nothing at all and three fills' worth of its rows measured a
    strategy that never ran. A payload that scales with the window cannot do that silently.
    """
    small = _plan(0.86, tool_share=0.6, context_limit=60_000)
    large = _plan(0.86, tool_share=0.6, context_limit=120_000)

    assert large.tool_result_tokens > small.tool_result_tokens
    assert abs(large.achieved_tool_share - small.achieved_tool_share) < 0.01, (
        "the same share must build the same workload at both sizes"
    )


def test_a_tool_share_with_no_room_for_the_rest_is_refused() -> None:
    """A share the non-tool floor cannot fit inside must fail, and say what to move.

    Nothing about a per-result size can rescue it: shrinking the results only takes the
    conversation further below its target, so the cell would quietly be built at a fill it
    never reached and read in the table as a strategy that failed to compact.
    """
    with pytest.raises(ValueError) as error:
        _plan(0.5, tool_share=0.97, context_limit=10_000)

    message = str(error.value)
    assert "tool share of 97%" in message
    assert "--tool-share" in message, "the error must name what to change"
    assert "5,000" in message, "the error must state the target it did not fit inside"


def test_an_impossible_tool_share_is_refused() -> None:
    """A share outside [0, 1) describes no payload.

    1.0 is excluded rather than clamped: a conversation that is nothing but tool results cannot
    be built, since the calls that produce them are not tool results themselves.
    """
    with pytest.raises(ValueError, match="tool_share"):
        _plan(0.5, tool_share=1.0)
    with pytest.raises(ValueError, match="tool_share"):
        _plan(0.5, tool_share=-0.1)


def test_a_tool_share_of_zero_sizes_exactly_as_it_did_before() -> None:
    """The old behaviour has to survive untouched, or every recorded cell becomes unreproducible.

    Six cells on disk were sized from a stated result size. If passing the new parameter's
    off value moved the sizing by a token, none of them could be re-run as measured.
    """
    stated, disabled = _plan(0.5), _plan(0.5, tool_share=0.0)

    assert disabled == stated
    assert disabled.tool_result_tokens == 500, "the stated size must survive the round trip"
    assert disabled.tool_share == 0.0


def test_the_tool_share_wins_over_a_stated_result_size() -> None:
    """Both together is a contradiction, and the share is the one that is resolved.

    Two ways of stating one quantity, so one of them has to lose. The share wins because it is
    the one that cannot be arrived at by hand: the size it derives depends on the window, the
    fill and the number of tool groups, which is the arithmetic the parameter exists to do.
    """
    shared = _plan(0.5, tool_share=0.4, tool_result_tokens=50)

    assert shared.tool_result_tokens != 50
    assert abs(shared.tool_share_deviation) <= FILL_TOLERANCE


def test_the_share_covers_the_code_free_results_too() -> None:
    """Asides are tool results, so they come out of the same budget rather than adding to it.

    They are what the strategies act on -- the anchored allowance divides by the tool groups in
    the band, bearing or not -- so a share that counted only the code-bearing ones would
    understate the payload by whatever the asides cost. The consequence, which is the reason
    this is pinned rather than assumed, is that turning asides on shrinks every result.
    """
    plain = _plan(0.86, tool_share=0.5, context_limit=120_000)
    with_asides = _plan(0.86, tool_share=0.5, context_limit=120_000, filler_tool_turns=6)

    assert abs(with_asides.tool_share_deviation) <= FILL_TOLERANCE
    assert with_asides.tool_result_tokens < plain.tool_result_tokens, (
        "the same budget over twice the results must make each result smaller"
    )


async def test_the_seeded_conversation_lands_on_the_requested_tool_share() -> None:
    """The share the run actually sent must be the share that was asked for.

    Checked against the assembled prompt rather than the plan's own arithmetic, for the reason
    the fill is: the stub walks what the agent put on the wire, which is an independent count
    of the same conversation.
    """
    plan = _plan(0.5, tool_share=0.45, tool_turns=6, markers_per_tool=8)
    scenario = build_live_scenario(
        salt="share",
        filler_turns=plan.filler_turns,
        filler_tokens=plan.filler_tokens,
        tool_turns=6,
        markers_per_tool=8,
        narration="neutral",
    )
    stub = SizingStubClient(reply=REPLY, obey_tool_choice=True)
    outcome = await run_live(
        ProviderRuntime(client=stub, model="stub"),
        strategy_name="none",
        options=StrategyOptions(TOKENIZER, 40_000, 2_048),
        scenario=scenario,
        tool_result_tokens=plan.tool_result_tokens,
        narration="neutral",
        probe_repeats=1,
    )

    # The seeded prompt by its billed size, so the tool half is read off the same call the
    # achieved fill is. A probe carries the snapshot plus a question and is a different total.
    seeded_tools = next(tools for total, tools in stub.sizes if total == outcome.seed_prompt_tokens)
    achieved = seeded_tools / outcome.seed_prompt_tokens
    assert abs(achieved - 0.45) / 0.45 <= FILL_TOLERANCE, (
        f"seeded {seeded_tools:,} tool tokens of {outcome.seed_prompt_tokens:,}, {achieved:.1%}"
    )
