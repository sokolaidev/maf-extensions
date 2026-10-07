"""Size the seeded conversation to a stated fraction of a stated context limit.

A cell in this matrix is "fill fraction X of a tried limit L". X decides how much irrelevant
context surrounds a fixed set of facts, so it is only a variable if the conversation actually
lands on X x L -- and only a *clean* variable if the payload it surrounds is held constant
while it moves.

Held constant in what sense is the second question, and it has two answers. Across one window
the payload is an absolute size, stated by ``tool_result_tokens``. Across two windows that
same absolute size is a shrinking share of the context, which is a different workload wearing
one label; ``tool_share`` states the payload as a share of the target instead and derives the
size, so cells at 60,000 and 120,000 tokens are the same workload at two scales.

The sizing is analytic rather than adaptive. The generator knows its own payload and filler
sizes and holds a tokenizer, so it can solve for the filler directly instead of running a
conversation, measuring it and adjusting. That is not merely cheaper: an adaptive loop would
have to calibrate against one strategy's run and then apply the result to the others, which
makes the user-side turn list depend on the order the strategies were measured in. Solving it
up front keeps that list identical across strategies by construction.

What cannot be solved for is the model's own replies, which are written live and are the one
term here that is assumed rather than computed. That is why the achieved fill is recorded on
the uncompacted run and checked against the target, rather than trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING, Final

from ._live import (
    DEFAULT_TOOL_RESULT_TOKENS,
    build_live_scenario,
    make_scope_tools,
    resolve_instructions,
)
from ._recall import RecallScenario

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_framework import TokenizerProtocol

__all__ = ["ASSUMED_REPLY_TOKENS", "MAX_FILL_FRACTION", "FillPlan", "plan_fill"]

ASSUMED_REPLY_TOKENS: Final[int] = 150
"""Tokens assumed for each reply the model writes during seeding.

Model replies cannot be sized in advance. The planner uses this allowance and the runner
reports achieved fill separately from the target.
"""

MAX_FILL_FRACTION: Final[float] = 10.0
"""Largest share of the context limit a seeded conversation may be sized to.

Above 1.0 on purpose: a cell whose control overflows the window is the one compaction exists
for. Well above it on purpose too: a small window under a long conversation is the only way
to reach a strategy's last resorts. See :func:`plan_fill` for what such a fill means and why
it stops at 10.0.
"""

_FUNCTION_CALL_TOKENS: Final[int] = 20
"""Serialized size of one assistant function-call message.

``{"call_id": ..., "name": "lookup_early", "arguments": "{}"}`` and nothing else: the scope is
pinned by which function is called, so no arguments travel.
"""

#: Automatic sizing adds equal padding to the scenario's three filler sections.
_SECTIONS: Final[int] = 3

#: Second point on the line the derived tool-result size is solved from. The first is a result
#: of one token, which is not zero tokens of tool payload: the codes, the preamble and the
#: function-call message are there whatever the body costs, and only the body scales.
_TOOL_PROBE_TOKENS: Final[int] = 1_000


@dataclass(frozen=True, slots=True)
class FillPlan:
    """How to build a seeded conversation that lands on a stated share of a context limit."""

    filler_turns: int
    filler_tokens: int
    context_limit: int
    """The limit this cell stands in for. Simulated: the model itself accepts 272,000."""
    fill_fraction: float
    target_tokens: int
    predicted_tokens: int
    """What the plan expects the seeded conversation to reach, replies included."""
    payload_tokens: int
    """Everything the conversation costs before any filler: the instructions, the turns that
    plant facts, every tool result, and the replies. The floor the fill has to clear."""
    tool_payload_tokens: int
    """The tool results alone, which is the part varied between runs and compared across them."""
    tool_result_tokens: int = 0
    """Size each tool result was built to, whether asked for directly or derived from a share.

    Defaulted only so that a plan read back from a file written before this field existed
    rebuilds; every plan this module produces sets it. A record from then carries the size on
    its cell parameters instead, which is where the reporting reads it.
    """
    tool_share: float = 0.0
    """Share of the target the tool results were sized to reach, 0 when the size was stated.

    The request, not the result: :attr:`achieved_tool_share` is what the sizing landed on.
    """

    @property
    def deviation(self) -> float:
        """Predicted fill against the target, as a signed fraction of the target."""
        return (
            (self.predicted_tokens - self.target_tokens) / self.target_tokens
            if self.target_tokens
            else 0.0
        )

    @property
    def achieved_tool_share(self) -> float:
        """Share of the predicted conversation that is tool-result text."""
        return self.tool_payload_tokens / self.predicted_tokens if self.predicted_tokens else 0.0

    @property
    def tool_share_deviation(self) -> float:
        """Predicted tool share against the requested one, as a signed fraction of the request."""
        return (
            (self.achieved_tool_share - self.tool_share) / self.tool_share
            if self.tool_share
            else 0.0
        )


def _tool_result_texts(
    scenario: RecallScenario,
    *,
    tool_result_tokens: int,
    narration: str,
    fact_placement: str,
) -> list[str]:
    """Return what each lookup tool actually hands back.

    Built from the same factory the run uses rather than from the scenario's scripted tool
    results, which live runs never send. Sizing against the scripted ones would miss the
    payload by whatever the two differ by, and they differ by the whole placement scheme.

    Returns:
        One result string per scope, in scope order.
    """
    return [
        tool()
        for tool in make_scope_tools(
            scenario.tool_lookups, tool_result_tokens, narration, fact_placement
        )
    ]


def _measure(
    scenario: RecallScenario,
    *,
    tokenizer: TokenizerProtocol,
    instructions: str,
    tool_result_tokens: int,
    narration: str,
    fact_placement: str,
    reply_tokens: int,
) -> tuple[int, int]:
    """Estimate the seeded conversation's size, and the tool results' share of it.

    Counts the material this package generates -- instructions, user turns, tool results, the
    function-call messages -- and assumes a flat size for the replies. It does not model the
    provider's per-message framing or the tool schemas, which together run a few hundred
    tokens: two orders below the tolerance the achieved fill is judged against, and both
    included in the achieved figure anyway.

    The closing questions are excluded. They are asked of the snapshot, one at a time, so they
    are not part of the context whose size is being set.

    Args:
        scenario: The scenario to size.

    Keyword Args:
        tokenizer: Token counter, the same one the strategies budget with.
        instructions: The agent instructions, which travel with every request.
        tool_result_tokens: Requested size of each tool result.
        narration: How hard the tool result asks for its values to be restated.
        fact_placement: Where the codes sit inside each result.
        reply_tokens: Assumed size of each reply the model writes.

    Returns:
        The estimated total, and the tool results' contribution to it.
    """
    turns = scenario.transcript.turns
    seed_turns = turns[: len(turns) - max(scenario.answer_turn_count, 1)]
    total = tokenizer.count_tokens(instructions)
    for turn in seed_turns:
        for message in turn.request:
            for content in message.contents:
                text = getattr(content, "text", None)
                if text:
                    total += tokenizer.count_tokens(text)
    total += len(seed_turns) * reply_tokens

    results = _tool_result_texts(
        scenario,
        tool_result_tokens=tool_result_tokens,
        narration=narration,
        fact_placement=fact_placement,
    )
    by_scope = dict(zip(sorted(scenario.tool_lookups), results, strict=False))
    tool_total = 0
    for index, scope in scenario.tool_turn_scopes.items():
        if index >= len(seed_turns):
            continue
        tool_total += tokenizer.count_tokens(by_scope.get(scope, "")) + _FUNCTION_CALL_TOKENS
    return total + tool_total, tool_total


def _solve_tool_result_tokens(
    measure: Callable[[int], tuple[int, int]],
    *,
    target: int,
    tool_share: float,
    context_limit: int,
    fill_fraction: float,
) -> int:
    """Solve for the per-result size that makes tool text ``tool_share`` of the target.

    Scaling payload with the target preserves workload proportions across context windows.
    The share includes code-free tool results, so adding those divides the same payload budget
    over more results.

    Args:
        measure: Sizes one candidate conversation at minimum filler, returning its estimated
            total and the tool results' share of that. The tool payload does not depend on
            the filler, so one filler setting is enough to solve against.

    Keyword Args:
        target: Tokens the seeded conversation is aiming at.
        tool_share: Share of that which should be tool-result text.
        context_limit: The limit the cell stands in for, for the error message.
        fill_fraction: The fill the target came from, for the error message.

    Returns:
        The size to build each tool result to.

    Raises:
        ValueError: If everything that is not a tool result already fills what the share
            leaves over -- in which case no per-result size satisfies the request, since
            shrinking the results only moves the conversation further below its target.
    """
    wanted = round(tool_share * target)
    base_total, base_tool = measure(1)
    floor = base_total - base_tool
    if floor >= target - wanted:
        raise ValueError(
            f"A tool share of {tool_share:.0%} leaves no room for the rest of the conversation: "
            f"{floor:,} tokens of instructions, fact-bearing turns and replies against the "
            f"{target - wanted:,} tokens left over from a target of {target:,} "
            f"({fill_fraction:.0%} of {context_limit:,}). Lower --tool-share, or raise --fill or "
            "--context-window so the target is larger. Reducing --tool-turns or "
            "--filler-tool-turns "
            "also lowers the floor, but it is mostly the turns that plant the facts and it "
            "does not "
            "shrink much."
        )
    # Two points on a line, for the reason the filler solve needs them: the body is generated
    # word by word to a character target, so its token count is proportional to the size asked
    # for but not equal to it, and the ratio depends on the tokenizer in use.
    probed = measure(_TOOL_PROBE_TOKENS)[1]
    per_token = max((probed - base_tool) / (_TOOL_PROBE_TOKENS - 1), 1e-6)
    result_tokens = max(round((wanted - base_tool) / per_token) + 1, 1)
    achieved = measure(result_tokens)[1]
    if achieved != wanted:
        result_tokens = max(result_tokens + round((wanted - achieved) / per_token), 1)
    return result_tokens


def plan_fill(
    *,
    tokenizer: TokenizerProtocol,
    context_limit: int,
    fill_fraction: float,
    salt: str = "plan",
    tool_turns: int = 6,
    filler_tool_turns: int = 0,
    markers_per_tool: int = 2,
    tool_result_tokens: int = DEFAULT_TOOL_RESULT_TOKENS,
    tool_share: float = 0.0,
    narration: str = "neutral",
    fact_placement: str = "spread",
    retrieval_guidance: bool = True,
    subset_questions: bool = True,
    filler_turn_tokens: int = 2_000,
    reply_tokens: int = ASSUMED_REPLY_TOKENS,
) -> FillPlan:
    """Solve for the filler that makes a seeded conversation reach ``fill_fraction`` of a limit.

    Filler is the dial and the payload is fixed. The number of filler turns is the coarse
    setting and their size the fine one, in that order on purpose: holding the size near a
    realistic 2,000 tokens and adding turns is what a longer conversation looks like, whereas
    holding the count and inflating each turn produces a handful of 40,000-token messages that
    no strategy would meet in practice.

    ``tool_share`` moves the payload from fixed to proportional. Fixed is what a matrix over
    one window wants; across windows it makes the payload shrink relative to the context, so
    the same configuration can be a heavy workload at 60,000 tokens and a light one at 120,000.
    Given a share, the per-result size is solved for first and the filler then fills whatever
    is left, exactly as before.

    A ``fill_fraction`` above 1.0 sizes the uncompacted conversation larger than the window the
    cell stands in for. That is the regime compaction exists for, and every cell measured below
    it was one where not compacting simply worked: the control is expected to disqualify, and
    the comparison becomes which compacting row keeps the run under the limit, and at what
    cost. The target is still ``context_limit * fill_fraction``, so nothing in the solve changes.
    Far past 1.0 is a design too. What a compacting row cannot remove -- chiefly the model's
    own replies, which no strategy here rewrites -- grows with the conversation, not with the
    window, so a small window under a long conversation is how a strategy is driven past its
    ordinary halves into its last resorts. At a 30,000-token window that takes a fill of about
    6 to 8, and the uncompacted control still has to fit the model's real limit to finish:
    272,000 tokens is a fill of about 9 at 30,000. The cap is 10.0 rather than none because a
    fraction written as a percentage -- 65 for 0.65 -- still has to be refused, and nothing
    between the two is a cell any model accepts at a window worth trying.

    Keyword Args:
        tokenizer: Token counter, the same one the strategies budget with.
        context_limit: The limit this cell stands in for.
        fill_fraction: Share of that limit the seeded conversation should reach, in
            ``(0.0, 10.0]``. Above 1.0 the control is sized to overflow the limit on purpose.
        salt: Cell-unique string for the probe scenarios built while solving. Immaterial to
            the answer, since markers are fixed-width whatever the salt.
        tool_turns: Tool-call groups to plant.
        filler_tool_turns: Extra lookups whose results carry no codes.
        markers_per_tool: Verifiable codes each tool result carries.
        tool_result_tokens: Requested size of each tool result. Ignored when ``tool_share``
            derives one, since the two state the same quantity two ways.
        tool_share: Share of the target that should be tool-result text, 0 to size each result
            from ``tool_result_tokens`` instead. See :func:`_solve_tool_result_tokens`. Defaulted
            to 0 here and to 0.6 by ``cachebench_live``, deliberately: a caller reaching this
            function directly states the sizing it wants, while the CLI has a matrix to keep
            comparable across windows and picks the proportional payload for it.
        narration: How hard the scenario pushes the model to restate tool values.
        fact_placement: Where the codes sit inside each result.
        retrieval_guidance: Whether the instructions carry the retrieval clause, which is part
            of every prompt and so part of the size.
        subset_questions: Whether the run closes with several targeted questions.
        filler_turn_tokens: Positive target size per filler turn. The solver chooses the
            number of turns from this and then adjusts their size to land exactly.
        reply_tokens: Assumed size of each reply. See :data:`ASSUMED_REPLY_TOKENS`.

    Returns:
        The plan, including what it predicts and what it is aiming at.

    Raises:
        ValueError: If either fraction is out of range, if the requested tool share leaves no
            room for the rest of the conversation, or if the payload alone does not fit inside
            the target -- in which case this cell cannot be built at all and no amount of
            adjusting the filler will change that.
    """
    if not 0.0 < fill_fraction <= MAX_FILL_FRACTION:
        raise ValueError(
            f"fill_fraction must be in (0.0, {MAX_FILL_FRACTION}]; got {fill_fraction}."
        )
    # A share of 1.0 is excluded rather than clamped: it asks for a conversation that is
    # nothing but tool results, which cannot be built because the calls that produce them are
    # not tool results themselves.
    if not 0.0 <= tool_share < 1.0:
        raise ValueError(f"tool_share must be in [0.0, 1.0); got {tool_share}.")
    if filler_turn_tokens <= 0:
        raise ValueError("filler_turn_tokens must be greater than 0.")
    target = round(context_limit * fill_fraction)
    instructions = resolve_instructions(narration, retrieval_guidance=retrieval_guidance)

    def size(groups: int, filler_tokens: int, result_tokens: int) -> tuple[int, int]:
        """Build one candidate conversation and estimate it.

        Returns:
            The estimated total, and the tool results' contribution to it.
        """
        scenario = build_live_scenario(
            salt=salt,
            filler_turns=groups * _SECTIONS,
            filler_tokens=max(filler_tokens, 1),
            tool_turns=tool_turns,
            filler_tool_turns=filler_tool_turns,
            markers_per_tool=markers_per_tool,
            narration=narration,
            subset_questions=subset_questions,
        )
        return _measure(
            scenario,
            tokenizer=tokenizer,
            instructions=instructions,
            tool_result_tokens=result_tokens,
            narration=narration,
            fact_placement=fact_placement,
            reply_tokens=reply_tokens,
        )

    # Each filler iteration places one extra lookup and one code-free aside per section.
    # The floor must leave enough iterations to place every requested result.
    per_section = max(tool_turns - _SECTIONS, filler_tool_turns, 0)
    minimum_groups = max(ceil(per_section / _SECTIONS), 1)

    if tool_share > 0:
        tool_result_tokens = _solve_tool_result_tokens(
            lambda result_tokens: size(minimum_groups, 1, result_tokens),
            target=target,
            tool_share=tool_share,
            context_limit=context_limit,
            fill_fraction=fill_fraction,
        )

    floor_tokens, tool_tokens = size(minimum_groups, 1, tool_result_tokens)
    if floor_tokens >= target:
        raise ValueError(
            f"The payload does not fit in this cell: {floor_tokens:,} tokens of instructions, "
            f"fact-bearing turns and tool results against a target of {target:,} "
            f"({fill_fraction:.0%} of {context_limit:,}). The payload is held fixed across the "
            "matrix and must fit inside the smallest cell, so reduce --tool-turns, "
            "--tool-result-tokens or --markers-per-tool rather than raising the fill here, "
            "which would make this cell incomparable with the others."
        )

    groups = max(minimum_groups, round((target - floor_tokens) / (filler_turn_tokens * _SECTIONS)))
    pairs = groups * _SECTIONS
    empty_tokens, _ = size(groups, 1, tool_result_tokens)
    # Two points on a line: the filler is generated word by word to a character target, so its
    # token count is proportional to the size asked for but not equal to it. Measuring the
    # ratio is what keeps this correct under a tokenizer other than the one it was written
    # against -- the character estimator counts this vocabulary at twice tiktoken's rate.
    probe_size = 500
    probed_tokens, _ = size(groups, probe_size, tool_result_tokens)
    per_token = max((probed_tokens - empty_tokens) / (pairs * probe_size), 1e-6)
    filler_tokens = max(round((target - empty_tokens) / (pairs * per_token)), 1)

    predicted, tool_tokens = size(groups, filler_tokens, tool_result_tokens)
    # One correction, because the two-point fit is not exact and a small systematic error on a
    # 230,000-token conversation is worth a second build. A third pass has never moved it.
    if predicted != target and pairs:
        filler_tokens = max(filler_tokens + round((target - predicted) / (pairs * per_token)), 1)
        predicted, tool_tokens = size(groups, filler_tokens, tool_result_tokens)

    return FillPlan(
        filler_turns=groups * _SECTIONS,
        filler_tokens=filler_tokens,
        context_limit=context_limit,
        fill_fraction=fill_fraction,
        target_tokens=target,
        predicted_tokens=predicted,
        payload_tokens=floor_tokens,
        tool_payload_tokens=tool_tokens,
        tool_result_tokens=tool_result_tokens,
        tool_share=tool_share,
    )
