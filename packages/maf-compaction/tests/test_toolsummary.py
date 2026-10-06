"""Tests for the record-then-drop strategy, its tool, and the middleware that forces it.

Two phases, and the tests separate them: phase 1 must ask without inserting anything into the
cached prefix, and phase 2 must act only on a record the *provider* issued -- never on one the
client invented, which is unsafe on routes that track tool calls server-side.

The tool's own text is tested here as well, because the middleware sends no message: the
description and the ``values`` guidance are the entire prompt for the record, so a clause
lost from them is a class of content silently dropped, with nothing else in the design to
catch it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from agent_framework import AgentSession, CharacterEstimatorTokenizer, ChatContext, Message
from agent_framework._compaction import included_token_count, project_included_messages

from maf_compaction._anchored import REMOVAL_MARKER, AnchoredCompactionStrategy
from maf_compaction._preserve import PRESERVE_REASON_KEY, is_preserved, set_preserved
from maf_compaction._toolsummary import (
    DEFAULT_COVERAGE_SHARE,
    DEFAULT_FALLBACK_FRACTION,
    DEFAULT_RECORD_MAX_TOKENS,
    DEFAULT_RECORD_TARGET_TOKENS,
    DEFAULT_TRIGGER_FRACTION,
    PRESERVE_REASON_UNCOVERED,
    PRESERVE_REASON_UNRECORDED,
    RECALL_TOOL_NAME,
    RECORD_MARKER,
    RecallGate,
    RecordDecisions,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    _distinctive_tokens,
    _hold_unrecorded,
    _Reforce,
    active_record_groups,
    build_record_message,
    find_record_index,
    make_recall_tool,
)

pytestmark = pytest.mark.anyio

TOKENIZER = CharacterEstimatorTokenizer()


#: Every arming the middleware performs, so a test can assert it happened exactly when the
#: tool was pinned and never otherwise.
_armings: list[int] = []


def _record_messages(values: str, *, call_id: str = "rec") -> list[Message]:
    """Return a matched recall call and result carrying ``values`` as a record.

    Separate from the conversation builder so a test can put two records in one conversation,
    which is what bounding the ask per record produces and what the strategy has to survive.

    Args:
        values: The record's text, as the model would have written it.

    Keyword Args:
        call_id: Distinguishes one record from another, and is what makes the pair count as
            provider-issued rather than client-invented.

    Returns:
        The two messages.
    """
    return [
        Message(
            role="assistant",
            contents=[
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": RECALL_TOOL_NAME,
                    "arguments": "{}",
                }
            ],
            message_id=f"{call_id}_call",
        ),
        Message(
            role="tool",
            contents=[
                {
                    "type": "function_result",
                    "call_id": call_id,
                    "result": f"{RECORD_MARKER} {values}",
                }
            ],
            message_id=f"{call_id}_res",
        ),
    ]


def _render_values(values: Sequence[str]) -> str:
    """Return values rendered the way a tool result renders them, as ``code_N=VALUE`` pairs.

    The shape is replicated here rather than imported. ``compaction/`` is meant to be lifted
    out whole, and its tests travel with it, so reaching into the benchmark for the renderer
    would be the boundary crossing ``test_boundary`` exists to refuse -- but the *shape* has to
    match, because a fixture that hands the strategy bare values tests a rendering no tool
    emits. That is the gap that let the coverage check ship measuring whether the model had
    copied the benchmark's label format: every value was compounded with its own label, no
    record quoting values plainly matched any of them, and 443 tests said nothing.

    Args:
        values: The verifiable values one tool result carries.

    Returns:
        A semicolon-separated list of labelled values.
    """
    return "; ".join(f"code_{index + 1}={value}" for index, value in enumerate(values))


def _conversation(
    tool_turns: int,
    payload_chars: int = 8_000,
    *,
    record: str | None = None,
    first_turn: int = 0,
    tool_name: str | None = None,
    result_values: Callable[[int], str] | None = None,
) -> list[Message]:
    """Return a conversation, optionally with a recall record the agent already made.

    Each turn calls a tool of its own, named ``lookup_<n>``. That is the live benchmark's own
    shape -- one no-argument tool per scope, so ``tool_choice`` can pin exactly which fact a
    turn gathers -- and it matters here because the strategy checks a record against the names
    of the tools it claims to cover. A fixture calling one tool eight times would exercise only
    the degenerate case, which ``tool_name`` builds deliberately instead.

    Args:
        tool_turns: How many lookup turns to generate.
        payload_chars: Size of each tool result's filler.

    Keyword Args:
        record: Values for a record appended after the turns, or None for no record.
        first_turn: Index the turns are numbered from, so two stretches can be concatenated
            without colliding on message ids, call ids or tool names.
        tool_name: One name shared by every turn, instead of a name per turn.
        result_values: What each turn's result carries in front of the filler, as a function of
            the turn index, instead of the default single ``CODE-<n>``. Coverage is now decided
            on the values a result contains, so a test has to be able to say what those are --
            and, by returning text with no digit in it, to build a result that yields no values
            at all and so falls through to the tool-name rule. Anything standing in for a
            *value* should be passed through :func:`_render_values`, because that is how a tool
            result presents one; anything standing in for prose should not.

    Returns:
        The messages.
    """
    messages = [
        Message(role="system", contents=["You are an assistant."], message_id="sys"),
        Message(role="user", contents=["Requirement: region is EU-WEST-1."], message_id="u0"),
        Message(role="assistant", contents=["Understood."], message_id="a0"),
    ]
    for offset in range(tool_turns):
        index = first_turn + offset
        call_id = f"call_{index}"
        values = result_values(index) if result_values else _render_values([f"CODE-{index}"])
        messages += [
            Message(role="user", contents=[f"Look up {index}."], message_id=f"u_{index}"),
            Message(
                role="assistant",
                contents=[
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": tool_name or f"lookup_{index}",
                        "arguments": "{}",
                    }
                ],
                message_id=f"a_call_{index}",
            ),
            Message(
                role="tool",
                contents=[
                    {
                        "type": "function_result",
                        "call_id": call_id,
                        "result": f"{values} " + "x" * payload_chars,
                    }
                ],
                message_id=f"t_res_{index}",
            ),
        ]
    if record is not None:
        messages += _record_messages(record)
    return messages


def _trailing(tool_turns: int, *, first_turn: int, narration: int = 0) -> list[Message]:
    """Return lookup turns to append behind a record, each optionally followed by narration.

    Since the fallback behind a record may shorten or shed no tool group a record does not
    cover -- the ones after it included -- assistant narration is the only thing it may still
    remove there. A fixture that needs the fallback to act has to give it some, and one that
    needs it to find nothing has to give it none; this is where that choice is made visibly.

    Args:
        tool_turns: How many lookup turns to generate.

    Keyword Args:
        first_turn: Index the turns are numbered from, continuing the stretch in front.
        narration: Characters of assistant narration after each turn, or 0 for none. Each
            reply's message id is ``n_<index>``.

    Returns:
        The messages, without the system prompt and opening exchange ``_conversation`` adds.
    """
    messages: list[Message] = []
    for index in range(first_turn, first_turn + tool_turns):
        messages += _conversation(tool_turns=1, first_turn=index)[3:]
        if narration:
            messages.append(
                Message(role="assistant", contents=["n" * narration], message_id=f"n_{index}")
            )
    return messages


def _covering_record(tool_turns: int, *, first_turn: int = 0) -> str:
    """Return a record that names every tool it covers, as the tool's own guidance asks.

    ``RECALL_VALUES_DESCRIPTION`` asks for the results "grouped by the tool that produced it",
    so this is what a compliant record looks like, and it is what the strategy checks against.

    The values are quoted bare, without the ``code_N=`` labels the results carry, because that
    is what "quote verbatim any value that cannot be reconstructed" produces and what every
    measured record actually looks like. A record has to cover a labelled result while writing
    plain values, or the check is measuring formatting compliance.

    Args:
        tool_turns: How many turns the record accounts for.

    Keyword Args:
        first_turn: Index those turns are numbered from.

    Returns:
        The record's text.
    """
    return " ".join(
        f"lookup_{index}: CODE-{index}." for index in range(first_turn, first_turn + tool_turns)
    )


def _sent(messages: list[Message]) -> set[str]:
    """Return the ids of the messages the model would receive."""
    return {message.message_id or "" for message in project_included_messages(messages)}


def _rendered(messages: list[Message]) -> str:
    """Return what the model would receive, tool results included."""
    parts: list[str] = []
    for message in project_included_messages(messages):
        for content in message.contents:
            result = getattr(content, "result", None)
            text = getattr(content, "text", None)
            parts.append(
                str(result) if result is not None else (text if text is not None else str(content))
            )
    return "\n".join(parts)


#: A ceiling that puts the eight-turn conversation between the two thresholds, so the default
#: strategy asks and waits rather than giving up. Chosen from the fixture's own size: the
#: conversation measures 17,011 tokens, 17,137 with a record, which is 78% of this -- between
#: the 60% trigger and the 90% give-up line, and not close to either.
#:
#: **Recompute it whenever a default moves, and check the margin rather than the sign.** A
#: fixture past the give-up line measures the fallback strategy; a fixture below the trigger
#: asserts against a strategy that returned without doing anything, and passes.
_WAITING_CEILING = 22_000


def _strategy(**kwargs: Any) -> ToolResultAnchoredSummarizationCompactionStrategy:
    kwargs.setdefault("max_input_tokens", _WAITING_CEILING)
    return ToolResultAnchoredSummarizationCompactionStrategy(tokenizer=TOKENIZER, **kwargs)


async def test_phase_two_drops_only_what_precedes_the_record() -> None:
    """The record is what those results were reduced to; everything after it is untouched."""
    strategy = _strategy()
    messages = _conversation(tool_turns=8, record=_covering_record(8))

    assert await strategy(messages) is True
    rendered = _rendered(messages)

    assert "lookup_0: CODE-0." in rendered, "the record itself survives"
    assert "x" * 100 not in rendered, "the bulk behind it is gone"
    assert "EU-WEST-1" in rendered, "the head anchor is never touched"
    assert strategy.records_found == 1


async def test_a_client_invented_record_is_not_trusted() -> None:
    """Only a result whose call the provider issued counts.

    A tool result with no matching call is what synthesising the pair client-side produces,
    and it is exactly what breaks on routes that track tool calls server-side. The strategy
    must not treat one as a record.
    """
    strategy = _strategy()
    messages = _conversation(tool_turns=8)
    messages.append(
        Message(
            role="tool",
            contents=[{"type": "function_result", "call_id": "invented", "result": "CODE-0"}],
            message_id="fake",
        )
    )

    await strategy(messages)

    assert strategy.records_found == 0
    assert "CODE-0 " in _rendered(messages), "nothing dropped on the strength of a bare result"


async def test_quoting_a_written_record_does_not_authorize_deletion() -> None:
    strategy = _strategy()
    messages = _conversation(tool_turns=8)
    record = build_record_message(_covering_record(8))
    messages.append(Message(role="assistant", contents=[record.text], message_id="quote"))
    before = _sent(messages)

    await strategy(messages)

    assert _sent(messages) == before
    assert strategy.records_found == 0
    assert find_record_index(messages) is None
    assert active_record_groups(messages) == []


async def test_a_written_records_provenance_survives_storage() -> None:
    record = build_record_message(_covering_record(8))
    restored = Message.from_dict(json.loads(json.dumps(record.to_dict())))
    messages = [*_conversation(tool_turns=8), restored]
    strategy = _strategy()

    assert find_record_index(messages) == len(messages) - 1
    assert await strategy(messages) is True
    assert strategy.records_found == 1
    assert "x" * 100 not in _rendered(messages)


async def test_nothing_happens_below_the_trigger() -> None:
    """A record that is not needed costs an agent turn and buys nothing."""
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=10_000_000, tokenizer=TOKENIZER
    )
    messages = _conversation(tool_turns=8)

    assert await strategy(messages) is False
    assert strategy.fallbacks_used == 0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_input_tokens": 0}, "max_input_tokens"),
        ({"max_input_tokens": 100, "trigger_fraction": 0.0}, "trigger_fraction"),
        ({"max_input_tokens": 100, "keep_head_groups": -1}, "keep_head_groups"),
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, Any], match: str) -> None:
    """A silently accepted bad bound produces a plausible-looking wrong measurement."""
    with pytest.raises(ValueError, match=match):
        ToolResultAnchoredSummarizationCompactionStrategy(tokenizer=TOKENIZER, **kwargs)


async def test_the_fallback_fires_when_the_record_never_arrives() -> None:
    """Waiting forever means overflowing the window, which is worse than truncating.

    The model may never call the tool. Past the fallback threshold the strategy stops asking
    and compacts without a record: the tool results are lost either way at that point, and
    the alternative is a provider error that loses the whole conversation.
    """
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=1_000, tokenizer=TOKENIZER, trigger_fraction=0.1, fallback_fraction=0.2
    )
    messages = _conversation(tool_turns=8)

    assert await strategy(messages) is True
    assert strategy.fallbacks_used == 1
    assert strategy.fallbacks_after_record == 0, (
        "no record arrived, so this cannot be the post-record path"
    )
    assert strategy.records_found == 0
    # It compacted rather than waiting further.
    assert "CODE-0" not in _rendered(messages), "the oldest results are gone"
    assert "EU-WEST-1" in _rendered(messages), "the fallback keeps the head anchor too"


@pytest.mark.parametrize(
    ("trigger", "fallback"),
    [pytest.param(0.8, 0.8, id="equal"), pytest.param(0.9, 0.8, id="inverted")],
)
async def test_thresholds_the_wrong_way_around_are_rejected(
    trigger: float, fallback: float
) -> None:
    """A fallback at or below the trigger silently disables the whole design.

    Equal is the quieter of the two and is why this is a range check rather than a comparison
    left to the caller: at equal thresholds the strategy passes the trigger and the give-up
    line on the same pass, so it never waits for a record at all and every row of that run
    measures the fallback while reporting the name of this strategy.
    """
    with pytest.raises(ValueError, match="fallback_fraction"):
        ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=1_000,
            tokenizer=TOKENIZER,
            trigger_fraction=trigger,
            fallback_fraction=fallback,
        )


async def test_a_record_that_does_not_free_enough_still_falls_back() -> None:
    """The groups after the record are untouched by design and can exceed the ceiling alone.

    The fallback still runs there. What it may take has narrowed to narration -- the tool
    groups behind the record are covered by no record and are held -- so the stretch behind
    the record carries some, or the fallback would have nothing and this would count zero.
    """
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=500, tokenizer=TOKENIZER, trigger_fraction=0.1, fallback_fraction=0.9
    )
    # The record sits early, so most of the bulk is behind it and survives phase 2. The second
    # stretch is numbered on from the first: reusing the numbers would give two groups the same
    # message ids, and the grouper derives its group ids from those.
    messages = _conversation(tool_turns=2, record=_covering_record(2))
    messages += _trailing(6, first_turn=2, narration=2_000)

    assert await strategy(messages) is True
    assert strategy.records_found == 1
    assert strategy.fallbacks_after_record == 1
    assert strategy.fallbacks_held_after_record == 1
    assert strategy.fallbacks_used == 0, "the give-up path is the one the record made unnecessary"


async def test_a_fallback_taken_behind_a_record_is_counted_apart_from_one_taken_without_one() -> (
    None
):
    """A pass that anchors on a record and falls back anyway reports that fallback.

    This pass finds a record, drops what the record covers, sees the prompt still over the
    ceiling, and hands the rest to the fallback, so part of the row is measured by another
    strategy and the flags column must say so. The count is separate from ``fallbacks_used``:
    no record ever arrived is a different event from one that arrived and did not free enough.

    The fallback may not touch groups the coverage check kept or tool groups behind the record,
    so the bulk here is narration *after* the record, and every tool result is asserted intact.
    """
    strategy = _strategy(max_input_tokens=500, trigger_fraction=0.1, fallback_fraction=0.9)
    messages = _conversation(tool_turns=4, record=_covering_record(2))
    messages += _trailing(4, first_turn=4, narration=2_000)

    assert await strategy(messages) is True

    assert strategy.fallbacks_after_record == 1
    assert strategy.fallbacks_used == 0, (
        "the give-up path never ran: a record was there and was anchored on"
    )
    assert strategy.groups_kept_uncovered == 2, (
        "two groups the record never quoted, and they are held"
    )
    rendered = _rendered(messages)
    assert "CODE-2 x" in rendered and "CODE-3 x" in rendered, (
        "the fallback may not touch what the check kept"
    )
    for index in range(4, 8):
        assert f"CODE-{index} x" in rendered, f"nor lookup_{index}, which no record covers"
    assert "n_4" not in _sent(messages), "it shed the narration behind the record instead"


async def test_a_fallback_that_changed_nothing_is_not_counted_as_one() -> None:
    """The flag says another strategy shortened part of this row, so a no-op must not raise it.

    A fallback with nothing left to shed returns False and touches nothing; the count follows
    that answer, so it counts losses rather than attempts.
    """
    calls = 0

    class _Inert(AnchoredCompactionStrategy):
        async def compact_to(self, messages: list[Message], *, ceiling: int) -> bool:
            nonlocal calls
            calls += 1
            return False

    strategy = _strategy(
        max_input_tokens=500,
        trigger_fraction=0.1,
        fallback_fraction=0.9,
        fallback=_Inert(max_input_tokens=500, tokenizer=TOKENIZER),
    )
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    assert await strategy(messages) is True, "the record still licensed two groups being dropped"

    assert calls == 1, "the fallback was still asked: it is the answer that is not a loss"
    assert strategy.fallbacks_after_record == 0
    assert strategy.fallbacks_used == 0


# region coverage, which is what a record is allowed to delete


async def test_a_partial_record_leaves_the_groups_it_never_named_in_place() -> None:
    """A record covering two groups of six may not delete the other four.

    This is the measured shape of gpt-5.6-luna: asked to record everything from six tool
    groups, it wrote about two. A record does not replace whatever precedes it, only what it
    names, and the unnamed groups are kept and counted. Neither the response cap nor the prompt
    changed the shortfall, so the check lives in the strategy.
    """
    strategy = _strategy(max_input_tokens=16_000)
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    assert await strategy(messages) is True
    rendered = _rendered(messages)

    assert "CODE-0 x" not in rendered, (
        "the record names lookup_0, so its bulk is genuinely redundant"
    )
    assert "CODE-1 x" not in rendered, "and lookup_1"
    for index in range(2, 6):
        assert f"CODE-{index} x" in rendered, (
            f"lookup_{index} is unmentioned, so its group stays whole"
        )
    assert strategy.groups_kept_uncovered == 4
    assert strategy.fallbacks_used == 0, "keeping more is not the same as failing to compact at all"


async def test_the_uncovered_count_describes_the_conversation_now_rather_than_its_history() -> None:
    """A shortfall a later record made good must stop being reported as a shortfall.

    The count is read as "this row is carrying groups a complete record would have replaced",
    and that is a statement about the prompt at the end of the run, not a total across passes:
    a second record that covers what the first missed takes the count back to zero.
    """
    strategy = _strategy(max_input_tokens=14_000)
    messages = _conversation(tool_turns=6, record="the lookups all completed.")

    assert await strategy(messages) is False, "a record quoting nothing licenses nothing"
    assert strategy.groups_kept_uncovered == 6

    messages += _record_messages(_covering_record(6), call_id="rec2")

    assert await strategy(messages) is True
    assert strategy.groups_kept_uncovered == 0, "the second record covered them, and they are gone"


async def test_a_complete_record_still_drops_every_group_it_covers() -> None:
    """The good-model path must not pay for the bad one.

    gpt-5.4-mini writes records that name every tool they cover, and on those the coverage
    check costs nothing: it is meant to be silent whenever the record did what it was asked.
    A check that also held back complete records would trade a rare silent loss for a constant
    one.
    """
    strategy = _strategy()
    messages = _conversation(tool_turns=8, record=_covering_record(8))

    assert await strategy(messages) is True

    assert "x" * 100 not in _rendered(messages), "every group the record named is gone"
    assert strategy.groups_kept_uncovered == 0
    assert strategy.fallbacks_used == 0
    assert strategy.fallbacks_after_record == 0, (
        "a record that freed enough needs no fallback behind it"
    )


async def test_a_newer_record_never_drops_an_older_one() -> None:
    """The only surviving account of what an old record covered is the old record.

    Bounding what one record must cover means a run takes several, and each ends up behind the
    next. Excluding a group merely because it precedes the newest record would delete the
    record before it: the same loss this strategy exists to prevent, one level removed and
    quieter, because a newer record that names the tools looks exactly like coverage.
    """
    strategy = _strategy(max_input_tokens=_TWO_RECORD_CEILING)
    messages = _conversation(tool_turns=2, record=f"older record. {_covering_record(2)}")
    messages += _conversation(tool_turns=2, first_turn=2)[3:]
    messages += _record_messages(f"newer record. {_covering_record(4)}", call_id="rec2")

    assert await strategy(messages) is True
    rendered = _rendered(messages)

    assert "older record." in rendered, "the newer record must not delete the older one"
    assert "newer record." in rendered
    assert "x" * 100 not in rendered, "the bulk the newer record does cover is still dropped"
    assert strategy.fallbacks_used == 0, (
        "the fallback sheds records of its own, so it must not be what ran"
    )


async def test_an_older_record_still_covers_what_it_carried_when_the_newer_one_says_nothing() -> (
    None
):
    """Coverage is read off every record the conversation still holds, not off the last one.

    Every record is preserved, so every record is still in the prompt and still answering for
    what it carries. Reading only the newest made that depend on which record happened to be
    last: a model writing *"already recorded above"* -- which is a reasonable thing to write,
    and cheaper than repeating itself -- left every group scoring as uncovered while a complete
    account of them sat one message earlier, and the row paid for keeping bulk that was
    genuinely redundant. The union is what the conversation actually still holds.

    The first two groups here are covered by the older record alone; the last two are covered by
    neither, and stay.
    """
    strategy = _strategy(max_input_tokens=_TWO_RECORD_CEILING)
    messages = _conversation(tool_turns=2, record=_covering_record(2))
    messages += _conversation(tool_turns=2, first_turn=2)[3:]
    messages += _record_messages("everything is already recorded above.", call_id="rec2")

    assert await strategy(messages) is True
    rendered = _rendered(messages)

    assert "CODE-0 x" not in rendered, "the older record quotes this group's value, and still does"
    assert "CODE-1 x" not in rendered
    assert "CODE-2 x" in rendered, "no record carries this one"
    assert "CODE-3 x" in rendered
    assert strategy.groups_kept_uncovered == 2
    assert strategy.fallbacks_used == 0


@pytest.mark.parametrize(
    ("record", "kept"),
    [
        pytest.param("lookup: both deployments were healthy.", 2, id="one-mention-for-two-groups"),
        pytest.param("lookup: healthy. lookup: healthy.", 0, id="one-mention-each"),
    ],
)
async def test_a_name_shared_by_two_valueless_groups_needs_a_mention_for_each(
    record: str, kept: int
) -> None:
    """Two calls to one tool are two groups and one name, which the name alone cannot resolve.

    A record grouped by tool -- which is what the tool's own guidance asks for -- names that
    tool once however many times it was called, so a single mention cannot say whether both
    calls were accounted for or only one. The count rule resolves the ambiguity by keeping
    both, and only a record that mentions the name once per group buys the drop. It errs
    towards keeping on purpose: the cost of that choice is tokens, and the cost of the other
    choice was a fact that vanished with no trace of where it went.

    The results here carry no digit anywhere, so they yield no distinctive values and the name
    rule is what decides them. That is now the *only* way to reach this rule, and it is why the
    ambiguity it works around is a smaller problem than it was: two calls to one tool return
    two different sets of values, so wherever there are values at all they say which call was
    accounted for and the name never has to.
    """
    strategy = _strategy(max_input_tokens=5_000)
    messages = _conversation(
        tool_turns=2,
        tool_name="lookup",
        record=record,
        result_values=lambda _: "the deployment is healthy",
    )

    await strategy(messages)
    rendered = _rendered(messages)

    assert strategy.groups_kept_uncovered == kept
    assert strategy.fallbacks_used == 0
    assert ("the deployment is healthy x" in rendered) is (kept == 2)


async def test_values_settle_two_calls_to_one_tool_that_the_name_cannot() -> None:
    """The name rule's own hard case dissolves once coverage is read off the values.

    Six calls to ``lookup_eu`` are six groups and one name, and a record grouped by tool writes
    that name once, so the count rule above has to keep all six unless the model repeats
    itself. The values do not have that problem: each call returned different ones, and a
    record quoting both sets has demonstrably accounted for both calls -- without ever writing
    the tool's name, which is the thing the models under measurement do not do.
    """
    strategy = _strategy(max_input_tokens=5_000)
    messages = _conversation(
        tool_turns=2,
        tool_name="lookup",
        result_values=lambda index: _render_values([f"AB-10000{index}", f"CD-20000{index}"]),
        record="first call returned AB-100000 and CD-200000; second returned AB-100001 and CD-200001.",
    )

    assert await strategy(messages) is True

    rendered = _rendered(messages)
    assert strategy.groups_kept_uncovered == 0, (
        "one mention of the name, and both groups still accounted for"
    )
    assert _render_values(["AB-100000", "CD-200000"]) not in rendered
    assert _render_values(["AB-100001", "CD-200001"]) not in rendered


def _codes(index: int) -> list[str]:
    """Return one deployment lookup's worth of identifiers, eight of them, as the live tool returns.

    Eight is the number the recall measurements were run at, and it is the number the default
    coverage share was chosen against: at eight values it tolerates exactly one being
    unrecognisable in the record.
    """
    return [f"{'ABCDEFGH'[index]}{'BCDEFGHI'[index]}-{123456 + offset:06d}" for offset in range(8)]


def _prose_record(index: int) -> str:
    """Return the record gpt-5.6-luna actually writes for one lookup.

    Verbatim in shape: the scope named in prose, every code quoted, and the function name
    ``lookup_<n>`` nowhere in the sentence. That is not a defective record -- it is a complete
    one, written the way models write -- and the tool-name rule scored it as covering nothing.
    """
    return f"extra{index} deployment lookup returned codes: " + ", ".join(_codes(index)) + "."


def _bulk_of(index: int) -> str:
    """Return a string that appears in one lookup's result and nowhere else, filler included."""
    return f"{_render_values(_codes(index))} x"


async def test_a_record_quoting_a_groups_values_covers_it_though_it_never_names_the_tool() -> None:
    """Coverage is read from the values a record quotes, not from the tool's name.

    Asked to record six tool groups, gpt-5.6-luna writes prose: *"extra0 deployment lookup
    returned codes: AB-123456, ..."*. Every identifier is there and ``lookup_extra0`` is not,
    so the group counts as covered. ``RECALL_VALUES_DESCRIPTION`` leads with "Quote verbatim any
    value that cannot be reconstructed or guessed", and that is what is tested.
    """
    strategy = _strategy(max_input_tokens=16_000)
    record = " ".join(_prose_record(index) for index in range(2))
    messages = _conversation(
        tool_turns=6, result_values=lambda index: _render_values(_codes(index)), record=record
    )

    assert "lookup_0" not in record, (
        "the fixture is only the luna case if the tool name is genuinely absent"
    )
    assert await strategy(messages) is True
    rendered = _rendered(messages)

    assert _bulk_of(0) not in rendered, (
        "the record carries every code this group returned, so its bulk can go"
    )
    assert _bulk_of(1) not in rendered
    for index in range(2, 6):
        assert _bulk_of(index) in rendered, (
            f"lookup_{index} is in no record, so its group stays whole"
        )
    assert strategy.groups_kept_uncovered == 4


async def test_a_record_that_names_a_tool_but_quotes_none_of_its_values_covers_nothing() -> None:
    """The other half of the same correction, and the reason the name was never the contract.

    A record can name every tool it was asked about and still have thrown away everything they
    returned. Under the name rule that record licensed deleting all six groups; under this one
    it licenses deleting none, because nothing it contains could answer a later question about
    what those calls found.
    """
    strategy = _strategy(max_input_tokens=16_000)
    messages = _conversation(
        tool_turns=6,
        result_values=lambda index: _render_values(_codes(index)),
        record="lookup_0, lookup_1, lookup_2, lookup_3, lookup_4 and lookup_5 all returned deployment codes.",
    )

    assert await strategy(messages) is False, "a record that quotes nothing buys nothing"

    assert strategy.groups_kept_uncovered == 6
    assert all(_bulk_of(index) in _rendered(messages) for index in range(6))


async def test_a_group_with_no_values_to_quote_falls_back_to_the_tool_name() -> None:
    """Where the value rule has nothing to read, the weaker rule decides -- not a default.

    A result holding only prose yields no distinctive values, so by this rule nothing in it is
    unreconstructable and the record cannot be checked against it. Both shortcuts are wrong.
    Calling such a group covered would let a record that mentions nothing delete a tool's
    findings, which is the silent loss the check exists to prevent; calling it uncovered would
    make every prose-only tool permanently undroppable, which is not conservatism but a broken
    strategy. So the name rule runs, and here it splits the six groups the way it should: the
    two the record names go, the four it does not stay.
    """
    strategy = _strategy(max_input_tokens=16_000)
    messages = _conversation(
        tool_turns=6,
        result_values=lambda _: "the deployment is healthy",
        record="lookup_0 and lookup_1 both reported healthy deployments.",
    )

    assert await strategy(messages) is True

    assert strategy.groups_kept_uncovered == 4, (
        "neither automatically covered nor automatically uncovered"
    )


async def test_a_record_quoting_bare_values_covers_a_result_that_labelled_them() -> None:
    """The live shape: labelled values in the result, bare values in the record.

    A tool result renders its values as ``code_N=VALUE``; a record quotes them plainly, because
    that is what "quote verbatim any value that cannot be reconstructed" asks for and what every
    measured record does. ``code_1=TL-BA44A9`` must yield ``TL-BA44A9`` as its own token, or
    coverage would measure formatting compliance rather than preservation.
    """
    strategy = _strategy(max_input_tokens=5_000)
    messages = _conversation(
        tool_turns=2,
        result_values=lambda index: _render_values([f"TL-BA44A{index}", f"TL-BB44A{index}"]),
        record="the two lookups returned TL-BA44A0, TL-BB44A0, TL-BA44A1 and TL-BB44A1.",
    )

    assert "code_1" not in messages[-1].contents[0].result, (
        "the record must not be quoting the labels"
    )
    assert await strategy(messages) is True

    assert strategy.groups_kept_uncovered == 0
    assert _render_values(["TL-BA44A0", "TL-BB44A0"]) not in _rendered(messages)


async def test_a_value_that_is_only_a_substring_of_the_record_does_not_count_as_quoted() -> None:
    """The coverage check was licensing the deletion it exists to prevent.

    ``value in record`` on lowercased text is not a test of whether the record carries the
    value. Replayed exactly: a group holding ``2026`` and ``1234`` was scored fully covered by
    *"ZZ-999999 was recorded on 2026-08-31 as AB-123456"*, in which neither value appears as a
    value at all -- ``2026`` inside a date, ``1234`` inside an unrelated identifier -- and the
    group was then deleted, with nothing anywhere preserving what it held. Any four-to-six digit
    number is a substring of some longer identifier or date, so this was not a corner case.

    Both sides are tokenised now, and membership is the test.
    """
    strategy = _strategy(max_input_tokens=5_000)
    messages = _conversation(
        tool_turns=2,
        result_values=lambda _: _render_values(["2026", "1234"]),
        record="ZZ-999999 was recorded on 2026-08-31 as AB-123456.",
    )

    assert await strategy(messages) is False, "a record carrying neither value licenses no deletion"

    assert strategy.groups_kept_uncovered == 2
    assert _rendered(messages).count(_render_values(["2026", "1234"])) == 2, (
        "both groups are still whole"
    )


@pytest.mark.parametrize(
    ("tool_name", "record"),
    [
        pytest.param(
            "get",
            "get_status reported healthy. get_status reported healthy.",
            id="get-inside-get_status",
        ),
        pytest.param(
            "read_file",
            "read_file_lines returned the head. read_file_lines returned the tail.",
            id="read_file-inside-read_file_lines",
        ),
    ],
)
async def test_a_tool_name_that_is_only_a_prefix_of_a_mentioned_one_is_not_a_mention(
    tool_name: str, record: str
) -> None:
    """A tool name counts as mentioned only as a whole name, and shared prefixes are the norm.

    A substring count of ``get`` is satisfied by ``get_status``, which would let a record about
    a different tool license deleting the groups of this one. Tool names share prefixes
    as a matter of course -- ``read_file`` and ``read_file_lines``, ``get`` and ``get_status``
    -- so this is what a real toolset looks like rather than a contrived collision. The record
    here even satisfies the *count*: it mentions the longer name once per group.

    The results carry no digit, so no value can be quoted and the name rule is what decides.
    """
    strategy = _strategy(max_input_tokens=5_000)
    messages = _conversation(
        tool_turns=2,
        tool_name=tool_name,
        record=record,
        result_values=lambda _: "the deployment is healthy",
    )

    assert await strategy(messages) is False

    assert strategy.groups_kept_uncovered == 2
    assert "the deployment is healthy x" in _rendered(messages)


@pytest.mark.parametrize(
    ("share", "kept"),
    [
        pytest.param(1.0, 2, id="every-value-or-nothing"),
        pytest.param(DEFAULT_COVERAGE_SHARE, 2, id="the-default-refuses-three-of-four"),
        pytest.param(0.75, 0, id="exactly-the-share-that-was-quoted"),
        pytest.param(0.0, 0, id="any-value-at-all"),
    ],
)
async def test_the_coverage_share_decides_how_much_of_a_group_must_be_quoted(
    share: float, kept: int
) -> None:
    """The dial has to move the trade-off across its whole range, or it is decoration.

    Each group returns four values and the record quotes three of them, so the boundary sits at
    exactly 0.75. The share is compared with ``ceil``, which is what makes it a genuine floor:
    at the 0.8 default a group of four needs all four, because three is 0.75 and 0.75 is less
    than 0.8. The two ends have to mean what they say as well -- 1.0 every value, 0.0 any value
    at all, the latter being the behaviour this check replaced and the row someone comparing
    the two would want to run.
    """
    strategy = _strategy(max_input_tokens=5_000, coverage_share=share)
    messages = _conversation(
        tool_turns=2,
        result_values=lambda index: _render_values([f"V{index}-{step}000" for step in range(1, 5)]),
        record=" ".join(f"V{index}-1000, V{index}-2000, V{index}-3000" for index in range(2)),
    )

    await strategy(messages)

    assert strategy.groups_kept_uncovered == kept
    assert ("V0-4000 x" in _rendered(messages)) is (kept == 2)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            'Deployment "AB-123456", version v1.2.3, updated 2026-08-31T09:00:00Z.',
            {"ab-123456", "v1.2.3", "2026-08-31t09:00:00z"},
            id="quoted-versioned-and-timestamped",
        ),
        pytest.param(
            "The region is EU-WEST and the status is healthy.", set(), id="no-digit-no-value"
        ),
        pytest.param("The 3rd of v2 at 10% (1).", set(), id="too-short-to-be-a-value"),
        pytest.param(
            "code_1=TL-BA44A9; code_2=TL-BA44A1; code_3=TL-BA44A2",
            {"tl-ba44a9", "tl-ba44a1", "tl-ba44a2"},
            id="labelled-pairs-yield-the-value-not-the-label",
        ),
        pytest.param(
            '{"id":"AB-123456","count":42,"seen":"2026-08-31T09:00:00Z"}',
            {"ab-123456", "2026-08-31t09:00:00z"},
            id="json",
        ),
        pytest.param(
            "deployment: AB-123456\nversion: v1.2.3\nregion: eu-west",
            {"ab-123456", "v1.2.3"},
            id="key-colon-space-value",
        ),
        pytest.param("AB-123456,CD-234567,healthy,42", {"ab-123456", "cd-234567"}, id="csv"),
        pytest.param(
            "'AB-123456' and \"CD-234567\" and [EF-345678]", {"ab-123456", "cd-234567", "ef-345678"}
        ),
        pytest.param(
            "token=SGVsbG8yMw==", {"sgvsbg8ymw"}, id="trailing-equals-is-not-a-value-boundary"
        ),
        pytest.param("id:AB-123456", {"id:ab-123456"}, id="unspaced-colon-is-not-separated"),
    ],
)
def test_the_value_rule_finds_what_cannot_be_reconstructed_and_leaves_prose_alone(
    text: str, expected: set[str]
) -> None:
    """The rule is stated so it can be argued with, and this is the statement executed.

    Split on whitespace and on the punctuation that separates values -- commas, semicolons,
    quotes, brackets, pipes -- read only the part after the last ``=``, strip punctuation from
    both ends of what remains, and keep it when at least four characters are left and one of
    them is a digit. Deliberately not fitted to this benchmark's hex codes: a rule that was
    would need refitting for every workload, which is the mistake the recall tool's own
    description already had to be rewritten out of.

    Three of these cases are the limits rather than the successes, and they are here to be
    argued with. An alphabetic value is invisible, which is why a group yielding nothing falls
    back to the tool name rather than being ruled either way. A colon is not a separator,
    because a timestamp is built out of colons and splitting on them would destroy the values
    the record is asked to quote -- so ``id: AB-1`` and ``{"id":"AB-1"}`` are separated by the
    space and the quotes, and bare ``id:AB-1`` is not separated at all. And ``=`` is read from
    the right, so a value whose only ``=`` is trailing padding keeps its whole self.
    """
    # The cases are written lowercased; which tokens are found does not depend on case.
    assert {token.lower() for token in _distinctive_tokens(text)} == expected


async def test_a_record_that_changes_a_values_case_does_not_cover_it() -> None:
    """A case-sensitive identifier quoted in another case is another value, so the group stays."""
    verbatim, lowered = _strategy(max_input_tokens=16_000), _strategy(max_input_tokens=16_000)

    await verbatim(_conversation(tool_turns=6, record=_covering_record(6)))
    await lowered(_conversation(tool_turns=6, record=_covering_record(6).lower()))

    assert verbatim.groups_kept_uncovered == 0, "the premise: quoted verbatim, it covers"
    assert lowered.groups_kept_uncovered == 6


# region protecting the record from the strategy behind it


#: The same choice as ``_WAITING_CEILING``, for the four-turn conversation carrying two
#: records: 8,769 tokens, which is 73% of this, between the 60% trigger and the 90% give-up
#: line. It was 10,000, which puts the same fixture at 88% -- inside the band, but two
#: percentage points from giving up, so a sentence added to a record's text would silently
#: change which strategy the test was measuring.
_TWO_RECORD_CEILING = 12_000

#: Padding that makes the record big enough for the fallback to want to trim it. The anchored
#: strategy's per-result floor is 150 tokens and this takes the record to about 590, so a
#: record left unprotected is cut rather than merely eligible to be.
_RECORD_PADDING = " ".join(
    f"deployment {index} returned code QQ-{100_000 + index}." for index in range(60)
)


async def test_the_record_survives_a_fallback_that_shortens_and_sheds_everything_else() -> None:
    """The fallback never shortens or sheds the record, the one thing the strategy exists to keep.

    Phase 2 deletes tool groups *because* the record replaced them. When the record does not
    free enough on its own, what remains goes to ``fallback`` -- by default
    ``AnchoredCompactionStrategy``, which shortens tool results and then sheds whole tool
    groups. The record is a tool result, and it is the only surviving copy of everything it
    licensed deleting, so it is held out of the fallback's reach.

    The fixture puts the record early enough to sit in the fallback's middle band -- the only
    place it can be touched -- and then squeezes the ceiling until the fallback runs hard.
    """
    strategy = _strategy(max_input_tokens=500, trigger_fraction=0.1, fallback_fraction=0.9)
    record = f"{_covering_record(2)} {_RECORD_PADDING}"
    messages = _conversation(tool_turns=2, record=record)
    messages += _trailing(6, first_turn=2, narration=2_000)

    assert await strategy(messages) is True
    rendered = _rendered(messages)

    assert strategy.fallbacks_after_record == 1, (
        "the fixture only means anything if the fallback ran"
    )
    assert f"{RECORD_MARKER} {record}" in rendered, "the record is intact to its last character"
    assert REMOVAL_MARKER not in rendered.split(RECORD_MARKER)[1], (
        "and carries no trim marker of its own"
    )
    assert "n_2" not in _sent(messages), "while the fallback shed what it could still reach"


async def test_every_record_is_marked_protected_including_the_ones_a_newer_record_supersedes() -> (
    None
):
    """An older record is the sole account of the groups behind it, so it is protected too.

    ``_drop_before`` already refuses to *delete* an older record. Without the mark the fallback
    would shorten it instead, which loses the same facts more quietly -- and the mark has to be
    re-applied on every pass, because compaction runs against a freshly loaded conversation and
    the annotations of the previous pass are not in it.
    """
    strategy = _strategy(max_input_tokens=_TWO_RECORD_CEILING)
    messages = _conversation(tool_turns=2, record=f"older record. {_covering_record(2)}")
    messages += _conversation(tool_turns=2, first_turn=2)[3:]
    messages += _record_messages(f"newer record. {_covering_record(4)}", call_id="rec2")

    await strategy(messages)

    assert {message.message_id for message in messages if is_preserved(message)} == {
        "rec_call",
        "rec_res",
        "rec2_call",
        "rec2_res",
    }


async def test_a_group_another_strategy_protected_is_neither_dropped_nor_reported_uncovered() -> (
    None
):
    """Phase 2 deletes, so it honours the mark too -- and does not confuse it with a shortfall.

    The mark means "this is the only surviving copy of something", and a record naming the tool
    that produced it does not change that. Counting the skip as ``groups_kept_uncovered`` would
    be worse than not skipping: that number is read as "the record fell short", and a protected
    group says nothing at all about the record.
    """
    strategy = _strategy(max_input_tokens=16_000)
    messages = _conversation(tool_turns=6, record=_covering_record(6))
    for message in messages:
        if message.message_id in {"a_call_1", "t_res_1"}:
            set_preserved(message, preserved=True, reason="test")

    assert await strategy(messages) is True
    rendered = _rendered(messages)

    assert "CODE-1 x" in rendered, "the protected group survives a record that covers it"
    assert "CODE-0 x" not in rendered, "while its unprotected neighbour does not"
    assert strategy.groups_kept_uncovered == 0, (
        "protected is not the same as the record falling short"
    )


async def test_an_uninvited_recall_call_is_not_protected_as_though_it_had_recorded_anything() -> (
    None
):
    """The gate refuses calls nobody asked for, and what it returns is not a record.

    The recall tool cannot be hidden from the model, so it is advertised on every request and
    was called unprompted on unpinned follow-ups in every early run. Protecting the result of
    such a call would give a message that preserves nothing the standing of one that preserves
    everything, and would hand the fallback one more thing it may not touch for no gain at all.
    """
    strategy = _strategy()
    messages = _conversation(tool_turns=8, record=_covering_record(8))
    messages += _record_messages(
        "Not required right now: nothing was recorded.", call_id="uninvited"
    )
    for message in messages:
        if message.message_id == "uninvited_res":
            message.contents[0].result = "Not required right now: nothing was recorded."

    await strategy(messages)

    assert {message.message_id for message in messages if is_preserved(message)} == {
        "rec_call",
        "rec_res",
    }


# region the two layers between an uncovered group and the fallback


def _held(messages: list[Message]) -> set[str]:
    """Return the ids of every message this strategy is holding out of its fallback's reach."""
    return {
        message.message_id or ""
        for message in messages
        if is_preserved(message)
        and message.additional_properties.get(PRESERVE_REASON_KEY) == PRESERVE_REASON_UNCOVERED
    }


def _result_text(messages: list[Message], message_id: str) -> str:
    """Return the tool result text carried by the message with ``message_id``."""
    (message,) = (message for message in messages if message.message_id == message_id)
    return "\n".join(
        str(content.result) for content in message.contents if content.type == "function_result"
    )


#: A ceiling the six-turn fixture never reaches, with the trigger low enough that every pass
#: runs. The chain tests below need a pass on every call -- an ask is judged by the passes
#: that follow it -- and a fixture that dips under the trigger after the record drops its two
#: groups would stop reaching the chain at all, and pass against any bound whatever.
_CHAIN_CEILING = 20_000


def _chained(**kwargs: Any) -> ToolResultAnchoredSummarizationCompactionStrategy:
    kwargs.setdefault("max_input_tokens", _CHAIN_CEILING)
    kwargs.setdefault("trigger_fraction", 0.1)
    return _strategy(**kwargs)


async def test_a_partial_record_asks_for_another_record_and_holds_what_it_missed() -> None:
    """Layer one: the pass that finds the shortfall asks for another record, and asks once.

    The coverage check keeps the groups a record failed to cover, and what it kept has to be
    out of the fallback's reach from the same pass, because the
    ask made here is answered two passes later and the fallback can run in between -- a group
    shortened while its record is in flight is a group that record can no longer quote. The
    ask itself is one-shot, like the gate on the other side of the middleware.
    """
    strategy = _chained()
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    assert await strategy(messages) is True

    assert strategy.groups_kept_uncovered == 4
    assert _held(messages) == {f"a_call_{index}" for index in range(2, 6)} | {
        f"t_res_{index}" for index in range(2, 6)
    }
    assert strategy.groups_preserved_uncovered == 0, (
        "held for an ask in flight is not yet preserved for good"
    )
    assert strategy.take_reforce() is True, (
        "the pass that found the shortfall asked for another record"
    )
    assert strategy.take_reforce() is False, "and asked once"


async def test_the_middleware_pins_the_call_after_the_strategys_ask_and_counts_it_apart() -> None:
    """The ask crosses to the middleware on a call exit and pins the next call, whatever else is true.

    The middleware holds no reference to the strategy, so the strategy's decision reaches it
    the way the gate's does: through one callable it asks on the exit of every call it did not
    pin. The size trigger is kept out of range here so the only thing that can pin is the ask,
    and repeats are turned off, because this must fire without them -- it asks on a measured
    shortfall, which a caller who turned repeats off for a complete-record model still needs.
    """
    _armings.clear()
    strategy = _chained()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=10_000_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        reforce=strategy.take_reforce,
        repeat_records=False,
    )
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    first = await _run(middleware, messages)
    await strategy(messages)
    second = await _run(middleware, messages)
    third = await _run(middleware, messages)
    fourth = await _run(middleware, messages)

    assert middleware.repeat_records is False
    assert "tool_choice" not in first, "nothing had been asked for yet"
    assert "tool_choice" not in second, (
        "the ask is taken on this call's exit and applied to the next"
    )
    assert third["tool_choice"] == {"mode": "required", "required_function_name": RECALL_TOOL_NAME}
    assert "tool_choice" not in fourth, "one ask, one pinned call"
    assert (middleware.forced_calls, middleware.reforced_calls) == (1, 1), (
        "counted as a forced call and as a re-force"
    )
    assert len(_armings) == 1


async def test_a_re_force_that_covers_the_rest_prevents_any_preservation() -> None:
    """The re-force fixing the shortfall: the held groups are released and dropped, nothing settles.

    This is the measured case on gpt-5.6-luna, whose record covers two of six groups and whose
    second covers the rest. A held group has to stay a candidate for the record it was held
    for, or the hold would be a permanent floor beside a complete account of it.
    """
    strategy = _chained()
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    await strategy(messages)
    assert strategy.take_reforce() is True
    await strategy(messages)
    assert strategy.groups_kept_uncovered == 4, (
        "the pinned call's own pass sees no record yet, and keeps holding"
    )
    messages += _record_messages(_covering_record(4, first_turn=2), call_id="rec2")

    assert await strategy(messages) is True

    assert strategy.groups_kept_uncovered == 0
    assert strategy.groups_preserved_uncovered == 0
    assert not _held(messages), "the holds came off with the coverage"
    assert "CODE-3 x" not in _rendered(messages), "and the groups were dropped, not shortened"
    assert strategy.take_reforce() is False, "nothing left to ask for"


async def test_a_re_force_that_fails_leads_to_preservation_and_the_fallback_cannot_shorten_those_groups() -> (
    None
):
    """Layer two: a record that covers none of what it was asked for settles the groups for good.

    Settled means preserved under this strategy's own reason for as long as the groups stay
    uncovered, and the fallback honours that mark exactly as it honours the record's. The
    material added behind the records at the end is what gives the fallback something it *may*
    take, so the test can show it ran -- ``fallbacks_after_record`` moves -- while the four held
    results stay intact to the last character. What is left is a prompt over the ceiling,
    which is the accepted consequence: the row reads ``DQ`` instead of losing a fact.

    That material is narration, not tool groups: a group after the record is covered by no
    record, so the fallback finds it held too.
    """
    strategy = _strategy(max_input_tokens=500, trigger_fraction=0.1, fallback_fraction=0.9)
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    await strategy(messages)
    assert strategy.take_reforce() is True
    await strategy(messages)
    messages += _record_messages("nothing further to record.", call_id="rec2")
    await strategy(messages)

    assert strategy.groups_preserved_uncovered == 4 == strategy.groups_kept_uncovered
    assert strategy.take_reforce() is False, "asking stopped helping, so it stopped"
    assert strategy.fallbacks_after_record == 0, "so far the fallback has had nothing it may take"

    messages += _trailing(4, first_turn=6, narration=2_000)

    assert await strategy(messages) is True

    rendered = _rendered(messages)
    assert strategy.fallbacks_after_record == 1, (
        "the fallback ran behind the record and found the new narration"
    )
    for index in range(2, 10):
        assert f"CODE-{index} x" in rendered, f"lookup_{index} is held, so it is still whole"
        assert REMOVAL_MARKER not in _result_text(messages, f"t_res_{index}"), (
            "and carries no trim marker"
        )
    assert "n_6" not in _sent(messages), "while narration nothing protected was shed"
    assert strategy.groups_preserved_uncovered == 4
    assert included_token_count(messages) > 500, (
        "the prompt is left over the ceiling, for the caller to see"
    )


def _held_unrecorded(messages: list[Message]) -> set[str]:
    """Return the ids of every message held because no record covered it when the fallback ran."""
    return {
        message.message_id or ""
        for message in messages
        if is_preserved(message)
        and message.additional_properties.get(PRESERVE_REASON_KEY) == PRESERVE_REASON_UNRECORDED
    }


async def _settled_behind_two_records(
    max_input_tokens: int,
) -> tuple[ToolResultAnchoredSummarizationCompactionStrategy, list[Message]]:
    """Return a strategy and conversation with four uncovered groups preserved for good.

    The first record covers two of six lookups, the re-forced one covers none, and layer two
    has settled the four it left. What a test appends behind that is what the post-record
    fallback then meets.
    """
    strategy = _strategy(
        max_input_tokens=max_input_tokens, trigger_fraction=0.1, fallback_fraction=0.9
    )
    messages = _conversation(tool_turns=6, record=_covering_record(2))
    await strategy(messages)
    assert strategy.take_reforce() is True
    await strategy(messages)
    messages += _record_messages("nothing further to record.", call_id="rec2")
    await strategy(messages)
    assert strategy.groups_preserved_uncovered == 4
    return strategy, messages


async def test_the_fallback_behind_a_record_may_not_shorten_a_tool_group_after_it() -> None:
    """No record covers a tool group after the newest one, so the fallback may not erode it.

    Four preserved groups, three lookups behind the records, and a prompt over the ceiling.
    The anchored fallback's tail is four groups, so lookup 6 sits just inside its band. With
    nothing but tool groups behind the records there is nothing the fallback may take, so it
    takes nothing: every result keeps
    every character, the prompt stays over the ceiling where the caller can see it, and the
    rule that held the fallback back says so. Repeated passes change nothing and end, because
    the fallback's shed loop stops on "nothing moved" rather than on "it fits".
    """
    strategy, messages = await _settled_behind_two_records(500)
    messages += _trailing(3, first_turn=6)
    whole = {index: _result_text(messages, f"t_res_{index}") for index in range(6, 9)}

    assert await strategy(messages) is False, "nothing it may take, so nothing is taken"

    for index, text in whole.items():
        assert _result_text(messages, f"t_res_{index}") == text, (
            f"lookup_{index} keeps every character"
        )
        assert f"t_res_{index}" in _sent(messages), f"and lookup_{index} is still sent"
    assert _held_unrecorded(messages) == {f"a_call_{index}" for index in range(6, 9)} | {
        f"t_res_{index}" for index in range(6, 9)
    }, "held under a reason of its own, apart from layer two's"
    assert _held(messages) == {f"a_call_{index}" for index in range(2, 6)} | {
        f"t_res_{index}" for index in range(2, 6)
    }
    assert strategy.fallbacks_held_after_record == 1, "the fallback was needed, and was held back"
    assert strategy.fallbacks_after_record == 0, (
        "and took nothing, which is not counted as a fallback"
    )
    assert included_token_count(messages) > 500, (
        "over the limit rather than a shortened result: the row reads DQ"
    )

    before = _rendered(messages)
    for _ in range(3):
        assert await strategy(messages) is False, "the shed loop ends when everything left is held"
    assert _rendered(messages) == before
    assert strategy.fallbacks_held_after_record == 4, (
        "each pass that needed the fallback and held it is counted"
    )
    assert included_token_count(messages) > 500


async def test_narration_is_all_the_fallback_behind_a_record_may_take_and_can_be_enough() -> None:
    """The fallback still runs behind a record, and a row it brings under the ceiling keeps every fact.

    Two uncovered groups in front of the record, four lookups behind it, and narration after
    two of those inside the fallback's band. The ceiling sits between the prompt with that
    narration and the prompt without it, so shedding narration alone fits the row: no tool
    result is shortened and no tool group is shed.
    """
    strategy = _strategy(max_input_tokens=14_000, trigger_fraction=0.1, fallback_fraction=0.9)
    messages = _conversation(tool_turns=4, record=_covering_record(2))
    messages += _trailing(2, first_turn=4, narration=8_000) + _trailing(2, first_turn=6)
    whole = {index: _result_text(messages, f"t_res_{index}") for index in range(2, 8)}

    assert await strategy(messages) is True

    sent = _sent(messages)
    assert {"n_4", "n_5"}.isdisjoint(sent), "the narration went"
    for index, text in whole.items():
        assert _result_text(messages, f"t_res_{index}") == text, (
            f"lookup_{index} keeps every character"
        )
        assert f"t_res_{index}" in sent, f"and lookup_{index} is still sent"
    assert (strategy.fallbacks_after_record, strategy.fallbacks_held_after_record) == (1, 1)
    assert included_token_count(messages) <= 14_000, "and that was enough: the row fits"


async def test_a_later_record_that_covers_a_group_held_behind_the_last_one_releases_it() -> None:
    """The hold is this strategy's own, and coverage lifts it exactly as it lifts layer two's.

    Behind the second record, lookups 6 to 8 are held because the fallback ran while no record
    covered them. A third record quoting lookups 6 and 7 licenses their deletion: they are
    released and dropped like any covered group. Lookup 8 it does not quote, so it is still
    uncovered -- now in front of a record, so the coverage check holds it under its own reason
    and asks for another record on its behalf, which is layer one starting over.
    """
    strategy, messages = await _settled_behind_two_records(500)
    messages += _trailing(3, first_turn=6)
    await strategy(messages)
    assert len(_held_unrecorded(messages)) == 6
    messages += _record_messages(_covering_record(2, first_turn=6), call_id="rec3")

    assert await strategy(messages) is True

    sent = _sent(messages)
    for index in (6, 7):
        assert f"t_res_{index}" not in sent, f"lookup_{index} was covered, so it was dropped"
        (result,) = (message for message in messages if message.message_id == f"t_res_{index}")
        assert not is_preserved(result), f"with no hold left on lookup_{index}"
    assert not _held_unrecorded(messages), "nothing is left behind the newest record to hold"
    assert {"a_call_8", "t_res_8"} <= _held(messages), (
        "lookup_8 is uncovered in front of it, and held as such"
    )
    assert strategy.take_reforce() is True, "and asked for"
    assert strategy.groups_kept_uncovered == 5


async def test_a_record_that_covers_everything_leaves_no_hold_and_no_held_fallback() -> None:
    """The normal path is untouched: nothing uncovered, no fallback, nothing held, nothing counted."""
    strategy = _strategy()
    messages = _conversation(tool_turns=8, record=_covering_record(8))

    assert await strategy(messages) is True

    assert "x" * 100 not in _rendered(messages), "every covered group was dropped"
    assert not _held_unrecorded(messages)
    assert not _held(messages)
    assert (strategy.fallbacks_after_record, strategy.fallbacks_held_after_record) == (0, 0)


async def test_an_ask_that_brings_no_record_ends_the_chain_on_the_pass_that_could_have_seen_it() -> (
    None
):
    """A pinned call that wrote nothing is the model declining, and the bound holds against it.

    The pinned call's own pass runs before the model writes anything, so it cannot see a
    record and must not be read as a failure; the pass after it -- the follow-up carrying the
    tool result -- is the first that can, and a record not there by then was not written. One
    pass too few would settle every ask on the call that was still answering it; one too many
    would hold the groups on the evidence of an ask that had already failed.
    """
    strategy = _chained()
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    await strategy(messages)
    assert strategy.take_reforce() is True

    await strategy(messages)
    assert strategy.groups_preserved_uncovered == 0, (
        "the pinned call's own pass is not yet a failed ask"
    )

    await strategy(messages)
    assert strategy.groups_preserved_uncovered == 4, (
        "the pass that could have seen the record, and did not"
    )
    assert strategy.take_reforce() is False

    for _ in range(5):
        assert await strategy(messages) is False
        assert strategy.take_reforce() is False, "no further ask, however many passes go by"
    assert _held(messages) == {f"a_call_{index}" for index in range(2, 6)} | {
        f"t_res_{index}" for index in range(2, 6)
    }


async def test_re_forcing_continues_while_each_record_helps_and_stops_on_the_first_that_does_not() -> (
    None
):
    """The bound on asking again is progress, and progress is finite because groups are.

    A re-force that produces another partial record must not re-arm for ever. Each ask that
    continues the chain has retired at least one group -- covered, and so dropped -- and the
    first ask that retires none ends it, so the asks made over one shortfall number at most
    the groups in it. Here the second record covers two of the four, which earns a third ask
    for the two it left; the third record covers nothing, and those two are settled.
    """
    strategy = _chained()
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    await strategy(messages)
    assert strategy.take_reforce() is True
    await strategy(messages)
    messages += _record_messages(_covering_record(2, first_turn=2), call_id="rec2")
    await strategy(messages)

    assert strategy.groups_kept_uncovered == 2
    assert strategy.groups_preserved_uncovered == 0
    assert strategy.take_reforce() is True, (
        "a record that helped earns another ask for what it left"
    )

    await strategy(messages)
    messages += _record_messages("already recorded above.", call_id="rec3")
    await strategy(messages)

    assert strategy.groups_preserved_uncovered == 2
    assert _held(messages) == {"a_call_4", "t_res_4", "a_call_5", "t_res_5"}
    for _ in range(5):
        await strategy(messages)
        assert strategy.take_reforce() is False, "a record that helped nothing ended the chain"
    assert strategy.records_in_conversation == 3


async def test_a_settled_group_a_later_record_quotes_is_released_and_dropped() -> None:
    """Settled is not a third state: the one thing that lifts a hold is coverage, and it still does.

    The middleware's own repeat rule can bring a record the chain never asked for, and a record
    that quotes a settled group's values licenses its deletion like any other. That is not the
    fallback resuming: the group leaves whole, and the count that said it was preserved stops
    saying so, because the count describes the prompt as it now stands.
    """
    strategy = _chained()
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    await strategy(messages)
    assert strategy.take_reforce() is True
    await strategy(messages)
    await strategy(messages)
    assert strategy.groups_preserved_uncovered == 4

    messages += _record_messages(_covering_record(4, first_turn=2), call_id="rec2")

    assert await strategy(messages) is True
    assert (strategy.groups_kept_uncovered, strategy.groups_preserved_uncovered) == (0, 0)
    assert not _held(messages)
    assert "CODE-5 x" not in _rendered(messages)


async def test_the_shed_loop_stops_when_everything_it_may_take_is_preserved_and_leaves_the_overflow_visible() -> (
    None
):
    """Preserving must be able to fail loudly and must never become a loop.

    With every candidate in the fallback's band held or a record, the shed loop drops nothing,
    breaks on its own "nothing moved" test, and returns False -- which no caller reads as "it
    fits": the pass leaves the prompt over the ceiling, changes nothing on the passes after,
    and the caller sends it as it stands. ``included_token_count`` is the only thing that says
    whether it fits, and here it says no.
    """
    strategy = _strategy(max_input_tokens=500, trigger_fraction=0.1, fallback_fraction=0.9)
    messages = _conversation(tool_turns=6, record=_covering_record(2))

    await strategy(messages)
    assert strategy.take_reforce() is True
    await strategy(messages)
    await strategy(messages)
    assert strategy.groups_preserved_uncovered == 4
    before = _rendered(messages)

    for _ in range(3):
        assert await strategy(messages) is False, "nothing may be removed, so nothing is"

    assert _rendered(messages) == before
    assert strategy.fallbacks_after_record == 0, (
        "a fallback that took nothing is not counted as one"
    )
    assert included_token_count(messages) > 500, (
        "and the overflow is left where the caller can see it"
    )


async def test_a_group_another_strategy_protected_is_not_mistaken_for_one_of_this_strategys_holds() -> (
    None
):
    """The hold is told apart by its reason, so another strategy's mark still takes the group out of the running.

    Skipping only marks that are not this strategy's is what lets a held group be released; it
    must not also let this strategy release, or count as uncovered, a group somebody else has
    declared irreplaceable.
    """
    strategy = _chained()
    messages = _conversation(tool_turns=6, record=_covering_record(6))
    for message in messages:
        if message.message_id in {"a_call_1", "t_res_1"}:
            set_preserved(message, preserved=True, reason="another_strategy")

    assert await strategy(messages) is True

    assert "CODE-1 x" in _rendered(messages), "protected elsewhere, so not dropped"
    assert strategy.groups_kept_uncovered == 0, "and not reported as the record falling short"
    assert strategy.take_reforce() is False, "nor asked for"
    assert not _held(messages)


class _Recorder:
    """Stands in for the rest of the pipeline, capturing the options a call went out with."""

    def __init__(
        self, context: ChatContext, messages: list[Message], finish_reason: str | None = None
    ) -> None:
        self.context = context
        self.messages = messages
        self.finish_reason = finish_reason
        self.seen: list[dict[str, Any]] = []

    async def __call__(self) -> None:
        from agent_framework import ChatResponse

        self.seen.append(dict(self.context.options or {}))
        self.context.messages = self.messages
        # What the provider says about why it stopped, which is the only thing that separates
        # a record the model chose to keep short from one it was cut off in the middle of.
        self.context.result = ChatResponse(
            messages=Message(role="assistant", contents=["ok"]), finish_reason=self.finish_reason
        )


async def _run(
    middleware: ToolResultRecallMiddleware,
    messages: list[Message],
    finish_reason: str | None = None,
    session: AgentSession | None = None,
) -> dict[str, Any]:
    """Drive one middleware pass and return the options the call went out with."""
    context = ChatContext(
        client=None,
        messages=[Message(role="user", contents=["q"])],
        options={"temperature": 0},
        session=session,
    )
    recorder = _Recorder(context, messages, finish_reason)
    await middleware.process(context, recorder)
    return recorder.seen[0]


async def test_the_middleware_forces_the_call_and_sends_no_message() -> None:
    """Phase 1 must leave no trace in the prompt, only in the options.

    A message appended here carries no history provider's source tag, so per-service-call
    persistence would treat it as new input and store it -- and an instruction of ours would
    then appear in the conversation the application replays to its user.
    """
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
    )
    big = _conversation(tool_turns=8)

    first = await _run(middleware, big)
    assert "tool_choice" not in first, "nothing is known about history size before the first call"

    second = await _run(middleware, big)
    assert second["tool_choice"] == {"mode": "required", "required_function_name": RECALL_TOOL_NAME}
    assert len(_armings) == 1, "the tool is armed exactly when it is pinned, and never otherwise"
    assert middleware.forced_calls == 1
    # The prompt is untouched: no instruction, no extra turn.
    assert all(
        "recall" not in str(m.contents[0]).lower() for m in [Message(role="user", contents=["q"])]
    )


async def test_forgetting_the_pending_decision_stops_the_next_call_forcing() -> None:
    """Restoring the snapshot has to clear the middleware's pending decision too.

    The decision to force a record is taken on one call and applied to the next, so it belongs
    to the conversation rather than to the middleware. Left in place, a decision taken while
    the conversation was being seeded fires on the first question asked of the snapshot and on
    none of the others -- one probe carrying a prompt the rest do not, which is exactly the
    difference between probes the snapshot design exists to remove.
    """
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
    )
    big = _conversation(tool_turns=8)

    # The first call arms nothing but leaves the middleware intending to force the next one.
    await _run(middleware, big)
    middleware.forget_pending()
    after = await _run(middleware, big)

    assert "tool_choice" not in after
    assert not _armings, "the recall tool was armed on a call the snapshot had reset"
    assert middleware.forced_calls == 0


async def test_the_middleware_refuses_a_second_session() -> None:
    """A decision taken for one session must not pin a call made for another."""
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
    )
    big = _conversation(tool_turns=8)

    await _run(middleware, big, session=AgentSession(session_id="a"))
    with pytest.raises(RuntimeError, match="once per session"):
        await _run(middleware, big, session=AgentSession(session_id="b"))

    assert not _armings, "the recall tool was armed for a session that never asked"
    middleware.forget_pending()
    released = await _run(middleware, big, session=AgentSession(session_id="b"))
    assert "tool_choice" not in released, "a restore releases the session binding"


async def test_a_forced_call_that_fails_is_forced_again_and_leaves_the_tool_disarmed() -> None:
    """The ask outlives a failed call, and the permission armed for it does not."""
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=gate.arm,
        disarm=gate.disarm,
        trigger_fraction=0.1,
    )
    big = _conversation(tool_turns=8)
    await _run(middleware, big)

    async def failing() -> None:
        raise ConnectionError("provider unavailable")

    context = ChatContext(client=None, messages=[Message(role="user", contents=["q"])], options={})
    with pytest.raises(ConnectionError):
        await middleware.process(context, failing)

    assert gate.take() is False, "no unpinned call may use the failed call's permission"
    assert middleware.forced_calls == 0
    retried = await _run(middleware, big)
    assert retried["tool_choice"]["required_function_name"] == RECALL_TOOL_NAME
    assert middleware.forced_calls == 1


async def test_a_permission_the_forced_call_left_unused_does_not_reach_the_next_call() -> None:
    """Armed through the forced call's exit, where the tool runs, and withdrawn on the next entry."""
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=gate.arm,
        disarm=gate.disarm,
        trigger_fraction=0.1,
    )
    big = _conversation(tool_turns=8)
    await _run(middleware, big)
    await _run(middleware, big)
    assert middleware.forced_calls == 1
    assert gate._armed, "the function layer runs the tool after the forced call returns"

    await _run(middleware, big)

    assert gate.take() is False, "the forced response wrote no record, so nothing may now"


def test_a_record_whose_result_is_split_from_its_call_is_still_protected() -> None:
    """A message between the call and its result puts them in two spans; both are the record."""
    call, result = _record_messages("CODE-0 CODE-1")
    messages = _conversation(tool_turns=2)
    messages += [call, Message(role="user", contents=["interjection"]), result]

    spans = [(group["start_index"], group["end_index"]) for group in active_record_groups(messages)]

    assert find_record_index(messages) == len(messages) - 1
    assert (len(messages) - 1, len(messages) - 1) in spans


def test_the_unrecorded_hold_covers_a_group_only_partly_preserved() -> None:
    """The fallback shortens result by result, so a protected call does not cover its result."""
    messages = _conversation(tool_turns=2)
    set_preserved(
        next(m for m in messages if m.message_id == "a_call_1"), preserved=True, reason="other"
    )

    _hold_unrecorded(messages)

    result = next(m for m in messages if m.message_id == "t_res_1")
    assert is_preserved(result)
    call = next(m for m in messages if m.message_id == "a_call_1")
    assert call.additional_properties[PRESERVE_REASON_KEY] == "other", "and its mark is kept"


def test_a_fallback_that_ignores_preservation_is_refused() -> None:
    """Only an anchored strategy honours the marks that keep the record out of its reach."""

    class _Framework:
        async def __call__(self, messages: list[Message]) -> bool:
            return False

    fallback: Any = _Framework()
    with pytest.raises(TypeError, match="AnchoredCompactionStrategy"):
        ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=1_000, tokenizer=TOKENIZER, fallback=fallback
        )


async def test_the_middleware_stops_once_a_record_exists() -> None:
    """Forcing a second record would re-drop what the first already covered."""
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
    )

    await _run(middleware, _conversation(tool_turns=8))
    await _run(middleware, _conversation(tool_turns=8, record="CODE-0"))
    after = await _run(middleware, _conversation(tool_turns=8, record="CODE-0"))

    assert "tool_choice" not in after
    assert find_record_index(_conversation(tool_turns=8, record="CODE-0")) is not None


async def test_the_middleware_leaves_small_conversations_alone() -> None:
    """Below the trigger there is nothing to record and nothing to drop."""
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=10_000_000, tokenizer=TOKENIZER, arm=lambda: _armings.append(1)
    )

    await _run(middleware, _conversation(tool_turns=8))
    after = await _run(middleware, _conversation(tool_turns=8))

    assert "tool_choice" not in after
    assert middleware.forced_calls == 0


async def test_a_single_record_is_attributed_exactly_once() -> None:
    """The transition must be tracked on the instance, not re-read from each prompt.

    Before the pipeline runs, context.messages holds only the new turn, so a pre-call check
    reports "no record" every time and every later call would count as another one.
    """
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
    )
    with_record = _conversation(tool_turns=8, record="CODE-0")

    for _ in range(5):
        await _run(middleware, with_record)

    assert middleware.records_forced + middleware.records_volunteered == 1


async def test_each_repeated_record_is_attributed_once() -> None:
    """A second record is a second arrival, however long the first stayed visible."""
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000, tokenizer=TOKENIZER, arm=lambda: None, trigger_fraction=0.1
    )
    first = _conversation(tool_turns=8, record="CODE-0")
    second = first + _record_messages("CODE-1", call_id="rec2")

    for conversation in (first, first, second, second):
        await _run(middleware, conversation)

    assert middleware.records_forced + middleware.records_volunteered == 2


async def test_one_trigger_event_forces_exactly_one_call() -> None:
    """One ask, one record.

    The decision is made on the way out of a call and applied to the next, so the exit of a
    *forced* call reads a history that predates the record it just asked for: the condition that
    fired still reads as true. Each surplus record is an agent turn, a broken prefix, and a
    permanent addition to the floor under the prompt.

    So a forced call decides nothing, and the call after it -- the first that can see the
    record -- decides on what is actually there.
    """
    _armings.clear()
    middleware = _repeating()
    big = _conversation(tool_turns=8)
    recorded = _conversation(tool_turns=8, record=_covering_record(8))

    calls = [
        await _run(middleware, big),
        await _run(middleware, big),
        # The record the forced call wrote is not in the loaded history until the next call.
        await _run(middleware, recorded),
        await _run(middleware, recorded),
    ]

    assert [("tool_choice" in options) for options in calls] == [False, True, False, False]
    assert middleware.forced_calls == 1
    assert len(_armings) == 1, "a second arming is a second record"


async def test_a_record_that_surfaced_after_the_forced_call_is_still_attributed_to_the_forcing() -> (
    None
):
    """Volunteering is a claim about the model, and it must not be made about our own ask.

    A forced call cannot see its own record, so the record surfaces on the call after it. Credit
    that call and every forced record reads as volunteered -- which is the difference between a
    mechanism and a coincidence, and is exactly what ``records_volunteered`` exists to keep
    apart. Before the middleware stopped re-deciding on a forced call's exit, this came out
    right only because the surplus second forced call was there to be credited.
    """
    _armings.clear()
    middleware = _repeating()
    big = _conversation(tool_turns=8)

    await _run(middleware, big)
    await _run(middleware, big)
    await _run(middleware, _conversation(tool_turns=8, record=_covering_record(8)))

    assert middleware.records_forced == 1
    assert middleware.records_volunteered == 0, "the middleware pinned the call that wrote it"


async def test_a_forced_call_that_wrote_no_record_is_asked_again_one_call_later() -> None:
    """Suppressing the re-ask is a deferral, not a surrender.

    A forced call can fail to produce a record -- cut off mid-arguments, or the option refused
    -- and the ask has to survive that. The suppression lasts exactly one call: the next one
    looks at the loaded history, finds nothing recorded, and asks again. A suppression that
    outlived the evidence would leave the strategy waiting for a record nobody was writing,
    until it gave up and compacted without one.
    """
    _armings.clear()
    middleware = _repeating()
    big = _conversation(tool_turns=8)

    calls = [await _run(middleware, big) for _ in range(4)]

    assert [("tool_choice" in options) for options in calls] == [False, True, False, True]
    assert middleware.forced_calls == 2


async def test_a_restored_snapshot_clears_the_outstanding_ask_as_well_as_the_pending_one() -> None:
    """A rewind takes the forced call with it, so the middleware must not still be waiting on it.

    ``forget_pending`` exists because the decision to force belongs to the conversation rather
    than to the middleware. The same is true of an ask already outstanding: restoring a snapshot
    taken before the forced call means the record that call was writing is not in the state
    being restored to, so a record appearing afterwards did not come from our ask, and crediting
    it to the forcing would claim a mechanism where there was a coincidence.
    """
    _armings.clear()
    middleware = _repeating()

    await _run(middleware, _conversation(tool_turns=8))
    await _run(middleware, _conversation(tool_turns=8))
    middleware.forget_pending()
    await _run(middleware, _conversation(tool_turns=8, record=_covering_record(8)))

    assert middleware.forced_calls == 1
    assert middleware.records_volunteered == 1, (
        "the ask that record would have answered was discarded"
    )
    assert middleware.records_forced == 0


# region the tool, which is the whole of the prompt


def _recall_exchange(result: str) -> list[Message]:
    """Return a matched recall call and result carrying ``result``."""
    return [
        Message(
            role="assistant",
            contents=[
                {
                    "type": "function_call",
                    "call_id": "r",
                    "name": RECALL_TOOL_NAME,
                    "arguments": "{}",
                }
            ],
        ),
        Message(
            role="tool", contents=[{"type": "function_result", "call_id": "r", "result": result}]
        ),
    ]


def test_the_recall_tool_records_only_while_armed() -> None:
    """The tool cannot be hidden, so it has to be inert instead.

    MAF requires it to be registered with the agent: the function-invocation layer wraps the
    middleware layer and builds its tool map first, so a tool supplied through per-call
    options reaches the model but never the executor, and the model's call goes unanswered.
    A registered tool is advertised on every request, and this one was called uninvited on
    every unpinned follow-up call. Permission is therefore separated from visibility.
    """
    gate = RecallGate()
    tool = make_recall_tool(gate)

    uninvited = tool("AA-1")
    gate.arm()
    armed = tool("AA-1")
    reused = tool("AA-2")

    assert RECORD_MARKER not in uninvited
    assert RECORD_MARKER in armed
    assert RECORD_MARKER not in reused, "one arming cannot licence a second record"
    # The scorer must agree, or an uninvited call would look like a record and the strategy
    # would drop results that nothing had preserved.
    assert find_record_index(_recall_exchange(uninvited)) is None
    assert find_record_index(_recall_exchange(armed)) is not None


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param("grouped by the tool that produced it", id="attribution"),
        pytest.param(
            "Quote verbatim any value that cannot be reconstructed or guessed",
            id="unreconstructable",
        ),
        pytest.param("Keep findings and conclusions as they were stated", id="findings"),
        pytest.param(
            "Carry over any summary a tool already produced as it stands", id="existing-summary"
        ),
        pytest.param("Summarise the remaining content briefly", id="the-rest"),
        pytest.param("keep exactness over brevity", id="tie-break"),
    ],
)
def test_the_tool_asks_for_every_kind_of_content_a_result_can_hold(clause: str) -> None:
    """The four instructions partition the content, and a missing one is a silent hole.

    The description and the ``values`` guidance are the entire prompt: the middleware sends no
    message, because one appended there would be persisted into the caller's own conversation.
    So whatever these do not name is content the model may drop without anything noticing, and
    the earlier text named only "identifiers and values seen in earlier tool results" -- fitted
    to this benchmark's hex codes, and blind to the prose, findings and conclusions that are
    most of what a real tool returns.
    """
    assert clause in (make_recall_tool().__doc__ or "")


def test_the_tool_says_what_the_call_is_for_before_it_says_what_to_pass() -> None:
    """The description has to stand on its own: a model reading the schema sees it first."""
    doc = make_recall_tool().__doc__ or ""

    assert doc.startswith("Record what must survive from earlier tool results")
    assert "after those results are removed from the conversation to save space" in doc


def test_the_target_length_is_stated_when_one_is_given() -> None:
    """The description is the only channel that makes the model plan for a size.

    A ``max_tokens`` cap cannot do it: a model does not shorten to fit one, it writes until it
    is cut, and on a tool call the cut lands inside the arguments JSON -- so a cap set where
    the record should end produces no record rather than a shorter one. The middleware cannot
    send an instruction message either, which leaves this text.
    """
    stated = make_recall_tool(target_tokens=1_500).__doc__ or ""
    default = make_recall_tool().__doc__ or ""
    silent = make_recall_tool(target_tokens=None).__doc__ or ""

    assert "1,500 tokens" in stated
    assert f"{DEFAULT_RECORD_TARGET_TOKENS:,} tokens" in default
    assert "Aim for about" not in silent
    # Stating a length must not cost a clause: the two bounds are independent instructions.
    assert "Quote verbatim any value that cannot be reconstructed or guessed" in silent


def test_the_target_sits_well_under_the_cap() -> None:
    """Overshooting the stated length must not be the same event as being cut off.

    They measure different things -- one is what the model aims for, the other is what the
    provider enforces -- and a default pair close together would make every slightly long
    record a truncated one.
    """
    assert DEFAULT_RECORD_TARGET_TOKENS < DEFAULT_RECORD_MAX_TOKENS


@pytest.mark.parametrize("target", [0, -1])
def test_a_nonpositive_record_target_is_rejected(target: int) -> None:
    with pytest.raises(ValueError, match="target_tokens"):
        make_recall_tool(target_tokens=target)


# region bounding the record


async def test_the_cap_is_set_on_the_forced_call_and_on_no_other() -> None:
    """Every other call needs the run's own cap; only this one is asked to write a record.

    Left to inherit ``--answer-max-tokens``, the single call instructed to summarise every
    earlier tool result is the one call in the run with no bound of its own.
    """
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
        record_max_tokens=777,
    )
    big = _conversation(tool_turns=8)
    recorded = _conversation(tool_turns=8, record="CODE-0")

    calls = [
        # Nothing is known about the history before the first call, so it cannot be forced.
        await _run(middleware, big),
        await _run(middleware, big),
        # The call after the forced one is where the record becomes visible, and it is not
        # itself pinned: one ask, one record.
        await _run(middleware, recorded),
        await _run(middleware, recorded),
    ]

    assert [("tool_choice" in options) for options in calls] == [False, True, False, False]
    for options in calls:
        assert ("max_tokens" in options) is ("tool_choice" in options), options
        assert options.get("max_tokens", 777) == 777
        # The call's other options survive: this replaces the option set, it does not discard it.
        assert options["temperature"] == 0


async def test_no_cap_leaves_the_runs_own_ceiling_in_place() -> None:
    """None has to mean what it meant before the parameter existed, or a run cannot opt out."""
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
        record_max_tokens=None,
    )
    big = _conversation(tool_turns=8)

    await _run(middleware, big)
    forced = await _run(middleware, big)

    assert "tool_choice" in forced
    assert "max_tokens" not in forced


def test_a_cap_that_cannot_hold_a_record_is_refused() -> None:
    """Zero is not "no cap": it is a call that can produce nothing, which None expresses."""
    with pytest.raises(ValueError, match="record_max_tokens"):
        ToolResultRecallMiddleware(
            max_input_tokens=1_000, tokenizer=TOKENIZER, arm=lambda: None, record_max_tokens=0
        )


async def test_a_record_cut_short_is_counted_rather_than_read_as_complete() -> None:
    """A partial record is the one failure this design does not otherwise show.

    A tool call cut mid-arguments produces no record at all, which is loud: the strategy waits,
    falls back, and the row carries FALLBACK. A call cut just after a closing brace yields a
    record that parses and looks whole -- and the strategy then drops every tool group behind
    something covering only part of them, so the loss is scored as compaction damage.
    """
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
    )
    big = _conversation(tool_turns=8)

    await _run(middleware, big, finish_reason="tool_calls")
    await _run(middleware, big, finish_reason="length")

    assert middleware.forced_calls == 1
    assert middleware.records_truncated == 1


async def test_only_the_forced_call_can_truncate_a_record() -> None:
    """Every call in a long run can hit its own ceiling; only one of them writes the record.

    Counting the rest would put an ordinary long answer in the column that says the record is
    partial, which is the reading the count exists to prevent.
    """
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
    )
    big = _conversation(tool_turns=8)

    # Every call stops at its ceiling, forced or not.
    for _ in range(4):
        await _run(middleware, big, finish_reason="length")

    assert middleware.forced_calls > 0
    assert middleware.records_truncated == middleware.forced_calls


async def test_a_forced_call_that_finished_is_not_reported_as_truncated() -> None:
    """A short record is a choice the model is allowed to make, and is not a cut one."""
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
    )
    big = _conversation(tool_turns=8)

    await _run(middleware, big, finish_reason="stop")
    await _run(middleware, big, finish_reason="tool_calls")

    assert middleware.forced_calls == 1
    assert middleware.records_truncated == 0


# region bounding what one record must cover


async def test_the_group_bound_forces_a_record_below_the_token_trigger() -> None:
    """Coverage does not scale with how much there is to cover, so the ask has to be bounded.

    Measured: gpt-5.6-luna's record covered two of six tool groups, and raising the response
    cap, raising the stated target and rewriting the prompt each left that unchanged. What was
    still within reach was asking each record for less. The ceiling here is far too large for
    the token trigger to fire at sixteen thousand tokens, so nothing but the group bound can
    have forced this call.
    """
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=10_000_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        max_groups_before_record=2,
    )
    big = _conversation(tool_turns=8)

    first = await _run(middleware, big)
    second = await _run(middleware, big)

    assert "tool_choice" not in first, "nothing is known about the history before the first call"
    assert second["tool_choice"] == {"mode": "required", "required_function_name": RECALL_TOOL_NAME}
    assert middleware.forced_calls == 1
    assert len(_armings) == 1, "the tool is armed exactly when it is pinned"


async def test_the_group_bound_asks_again_once_the_next_groups_have_accumulated() -> None:
    """One record covers a bounded stretch, and the stretch after it needs its own.

    The token trigger cannot be the thing that asks for a second record: the size that fired it
    does not go away when the record arrives, so it would pin every remaining call in the run.
    The group bound is what re-arms, and it counts from the newest record rather than from the
    start -- so a conversation that has done no tool work since its record is left alone.
    """
    _armings.clear()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=10_000_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        max_groups_before_record=2,
    )
    settled = _conversation(tool_turns=8, record=_covering_record(8))
    grown = [*settled, *_conversation(tool_turns=2, first_turn=8)[3:]]

    await _run(middleware, settled)
    quiet = await _run(middleware, settled)
    await _run(middleware, grown)
    again = await _run(middleware, grown)

    assert "tool_choice" not in quiet, "no work has happened since the record, so nothing is due"
    assert "tool_choice" in again, "two groups have, so the next record is"
    assert middleware.forced_calls == 1


async def test_without_the_group_bound_the_middleware_asks_once_and_stops() -> None:
    """None has to mean what it meant before the parameter existed, on both halves.

    Off, tool groups are not a trigger at all -- eight of them below the token threshold force
    nothing -- and a record that exists ends the asking for good. Passing None explicitly has
    to be identical to not passing it, or a run cannot opt out of the new behaviour.
    """
    _armings.clear()
    unbounded = ToolResultRecallMiddleware(
        max_input_tokens=10_000_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        max_groups_before_record=None,
    )
    big = _conversation(tool_turns=8)

    await _run(unbounded, big)

    assert "tool_choice" not in await _run(unbounded, big), (
        "eight groups trigger nothing when the bound is off"
    )
    assert unbounded.forced_calls == 0
    assert not _armings

    asking = ToolResultRecallMiddleware(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        arm=lambda: _armings.append(1),
        trigger_fraction=0.1,
        max_groups_before_record=None,
    )
    recorded = _conversation(tool_turns=8, record=_covering_record(8))

    await _run(asking, big)
    forced = await _run(asking, big)
    await _run(asking, recorded)
    settled = await _run(asking, recorded)

    assert "tool_choice" in forced, "the token trigger still fires"
    assert "tool_choice" not in settled, "and still stops for good once a record exists"


@pytest.mark.parametrize("bound", [0, -1])
def test_a_group_bound_that_can_never_hold_a_record_is_refused(bound: int) -> None:
    """Zero groups per record is a record forced on every call, which is not a bound.

    A silently accepted bad bound produces a plausible-looking wrong measurement: the run would
    spend an agent turn on a record before every single call and report the result as this
    design's cost.
    """
    with pytest.raises(ValueError, match="max_groups_before_record"):
        ToolResultRecallMiddleware(
            max_input_tokens=1_000,
            tokenizer=TOKENIZER,
            arm=lambda: None,
            max_groups_before_record=bound,
        )


# region repeating the record


def _repeating(**kwargs: Any) -> ToolResultRecallMiddleware:
    """Return a middleware whose token trigger fires on the eight-turn fixture from call one.

    The ceiling is small and the trigger low, so size alone is above the line throughout. That
    is the point: every test below is about what happens once size has stopped being the
    interesting variable.

    Repeats are asked for by name, so these tests do not depend on the constructor's default.

    Keyword Args:
        kwargs: Overrides, so a test can turn repeats off again or add a group bound.

    Returns:
        The middleware.
    """
    kwargs.setdefault("max_input_tokens", 1_000)
    kwargs.setdefault("trigger_fraction", 0.1)
    kwargs.setdefault("repeat_records", True)
    return ToolResultRecallMiddleware(tokenizer=TOKENIZER, arm=lambda: _armings.append(1), **kwargs)


async def test_no_record_is_forced_while_nothing_new_has_been_recorded_since_the_last_one() -> None:
    """A settled conversation is not forced on every call, however far above the trigger it sits.

    The size that fires the trigger does not go away when a record arrives, because the record
    is *added* to the conversation and then preserved, so the prompt is if anything larger
    afterwards. Re-arm on size alone and every remaining call in the run is pinned to the
    recall tool -- an agent turn each, a broken prefix each, and a conversation of records
    about records.

    What re-arms the trigger is therefore new material rather than size. A conversation sitting
    far above the trigger with nothing recorded since its last record is settled, and five
    calls in a row have to leave it alone.
    """
    _armings.clear()
    middleware = _repeating()
    settled = _conversation(tool_turns=8, record=_covering_record(8))

    calls = [await _run(middleware, settled) for _ in range(5)]

    assert not [options for options in calls if "tool_choice" in options], (
        "a settled conversation was pinned"
    )
    assert middleware.forced_calls == 0
    assert not _armings


async def test_a_second_record_is_forced_once_new_groups_have_accumulated_above_the_trigger() -> (
    None
):
    """One record covers what was there when it was written, and nothing after it.

    Without repeats, every tool group gathered after the first record is uncoverable for the
    rest of the run: the strategy will not delete what no record carries, so those groups sit
    in the prompt to the end and the row reports ``UNCOVERED`` for work no record was ever
    asked to account for. The size trigger has to be able to ask again -- and it may, because
    the conversation has done something since.
    """
    _armings.clear()
    middleware = _repeating()
    settled = _conversation(tool_turns=8, record=_covering_record(8))
    grown = [*settled, *_conversation(tool_turns=2, first_turn=8)[3:]]

    quiet = await _run(middleware, settled)
    await _run(middleware, grown)
    again = await _run(middleware, grown)

    assert "tool_choice" not in quiet
    assert again["tool_choice"] == {"mode": "required", "required_function_name": RECALL_TOOL_NAME}
    assert middleware.forced_calls == 1
    assert len(_armings) == 1, "the tool is armed exactly when it is pinned"


async def test_a_single_group_of_new_work_is_enough_to_ask_again() -> None:
    """The bar is "something happened", not "enough happened".

    ``max_groups_before_record`` is the knob for how much one record should be asked to cover.
    Setting the bar higher here would duplicate that knob at a value nobody chose, and this
    condition exists for one purpose only: keeping the trigger off a conversation in which
    nothing has changed.
    """
    _armings.clear()
    middleware = _repeating()
    settled = _conversation(tool_turns=8, record=_covering_record(8))
    grown = [*settled, *_conversation(tool_turns=1, first_turn=8)[3:]]

    await _run(middleware, grown)
    again = await _run(middleware, grown)

    assert "tool_choice" in again


async def test_turning_repeats_off_reproduces_the_single_record_run_exactly() -> None:
    """Runs 26-39 were single-record, and a row compared against them has to be one too.

    Not a historical note: those cells are on disk and are what the write-ups quote, and a
    strategy that now takes three records where they took one has a different cost profile --
    each record is an agent turn, and each is preserved for the rest of the run. Reproducing
    them has to be one flag rather than a reconstruction, or the comparison stops being made.
    """
    _armings.clear()
    asking = _repeating(repeat_records=False)
    big = _conversation(tool_turns=8)

    await _run(asking, big)

    assert "tool_choice" in await _run(asking, big), "the first record is still asked for"

    _armings.clear()
    middleware = _repeating(repeat_records=False)
    settled = _conversation(tool_turns=8, record=_covering_record(8))
    grown = [*settled, *_conversation(tool_turns=4, first_turn=8)[3:]]

    await _run(middleware, settled)
    await _run(middleware, grown)
    after = await _run(middleware, grown)

    assert "tool_choice" not in after, "and no later one is, however much work has piled up"
    assert middleware.forced_calls == 0
    assert not _armings


async def test_a_group_bound_keeps_forcing_records_even_with_repeats_switched_off() -> None:
    """Setting the bound is asking for repeats outright, so the flag must not make it inert.

    The two settings answer different questions -- one is "reproduce the older runs", the other
    is "stop asking any one record to cover more than N groups" -- and a flag that quietly
    disabled the bound would leave a run reporting a bound it was not applying.
    """
    _armings.clear()
    middleware = _repeating(repeat_records=False, max_groups_before_record=2)
    settled = _conversation(tool_turns=8, record=_covering_record(8))
    grown = [*settled, *_conversation(tool_turns=2, first_turn=8)[3:]]

    await _run(middleware, grown)
    again = await _run(middleware, grown)

    assert "tool_choice" in again
    assert middleware.forced_calls == 1


async def test_the_strategy_counts_every_record_the_conversation_carries() -> None:
    """Records accumulate and nothing merges them, so the count is the whole of the warning.

    Every record is preserved -- unshrinkable, undroppable, counted against the ceiling in
    full -- so each one raises a floor under the prompt that no later pass can lower. Nothing
    else in the run says so: the message count keeps rising and each pass still reports having
    compacted. ``records_found`` cannot say it either, because it saturates at one and answers
    whether the model ever complied.
    """
    strategy = _strategy(max_input_tokens=_TWO_RECORD_CEILING)
    messages = _conversation(tool_turns=2, record=f"older record. {_covering_record(2)}")
    messages += _conversation(tool_turns=2, first_turn=2)[3:]
    messages += _record_messages(f"newer record. {_covering_record(4)}", call_id="rec2")

    await strategy(messages)

    assert strategy.records_in_conversation == 2
    assert strategy.records_found == 1, "compliance is a different question from quantity"


async def test_the_record_count_is_a_maximum_rather_than_a_running_total() -> None:
    """The same conversation is re-examined on every later pass, so a tally would multiply it.

    **The fixture has to stay above the trigger.** Records that covered their groups would drop
    enough to put the conversation below it, every later pass would return before reaching the
    counter, and the test would pass against ``+=`` as well as ``max``. These records quote
    nothing, so nothing is dropped and all four passes reach the count.
    """
    strategy = _strategy(max_input_tokens=_TWO_RECORD_CEILING)
    messages = _conversation(tool_turns=2, record="older record, quoting nothing.")
    messages += _conversation(tool_turns=2, first_turn=2)[3:]
    messages += _record_messages("newer record, quoting nothing.", call_id="rec2")

    for _ in range(4):
        assert await strategy(messages) is False, (
            "nothing here is covered, so nothing may be dropped"
        )

    assert included_token_count(messages) > int(_TWO_RECORD_CEILING * DEFAULT_TRIGGER_FRACTION), (
        "a fixture that falls below the trigger stops reaching the counter, and this stops testing it"
    )
    assert strategy.records_in_conversation == 2


def test_the_default_thresholds_leave_a_whole_turn_for_the_record_to_arrive_in() -> None:
    """The two defaults are one decision, and moving either alone breaks the design.

    The record arrives one call late by construction: the middleware can only read the history
    on the way out of a call and can only pin the next one. So the gap between asking and
    giving up has to be wide enough for a turn's growth to fit inside it, and at 0.6/0.9 it is
    three tenths of the ceiling. The strategy and the middleware read the same constant for the
    ask, so a run cannot move one and not the other.

    **These are the values every archived run used, and that is the point of pinning them.** A
    row is only comparable with the archive if it was taken under the same configuration.
    """
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=1_000, tokenizer=TOKENIZER
    )
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1_000, tokenizer=TOKENIZER, arm=lambda: None
    )

    defaults = (DEFAULT_TRIGGER_FRACTION, DEFAULT_FALLBACK_FRACTION)

    assert (strategy.trigger_fraction, strategy.fallback_fraction) == defaults
    assert (DEFAULT_TRIGGER_FRACTION, DEFAULT_FALLBACK_FRACTION) == (0.6, 0.9), (
        "runs 26-39 were taken at 0.6/0.9"
    )
    assert middleware.trigger_fraction == strategy.trigger_fraction, (
        "the ask and the wait must be one number"
    )
    assert strategy.fallback_fraction - strategy.trigger_fraction >= 0.1, (
        "no room for the record to land in"
    )


def test_decisions_taken_as_a_value_put_the_strategy_back_and_share_nothing_with_it() -> None:
    """A re-entry from a snapshot must start from the decisions standing then, not those made since.

    The settled groups, the preserved ones and the outstanding ask live on the instance and
    survive a session restore on their own. A value taken before the first re-entry and put back
    before each later one is what makes the re-entries repeats of each other; a value that
    aliased the instance's own sets would be rewritten by the re-entry it was meant to undo.
    """
    strategy = _strategy(max_input_tokens=10_000)
    strategy._uncovered = {"g1", "g2"}
    strategy._settled = {"g1"}
    strategy._preserved = {"g1"}
    strategy._reforce = _Reforce(targets=frozenset({"g2"}), records=1, passes=1)

    taken = strategy.decision_state()
    assert isinstance(taken, RecordDecisions)

    strategy._uncovered.add("g3")
    strategy._settled |= {"g2", "g3"}
    strategy._preserved = set(strategy._settled)
    assert strategy._reforce is not None
    strategy._reforce.passes += 1
    strategy._reforce = None

    assert taken.settled == frozenset({"g1"}), "the value shared a set with the instance"
    assert taken.reforce is not None and taken.reforce.passes == 1, (
        "the value shared the ask with the instance"
    )

    strategy.restore_decisions(taken)

    assert strategy._uncovered == {"g1", "g2"}
    assert strategy._settled == {"g1"}
    assert strategy._preserved == {"g1"}
    assert strategy._reforce == _Reforce(targets=frozenset({"g2"}), records=1, passes=1)
    assert strategy._reforce is not taken.reforce, (
        "restoring handed the instance the value's own ask"
    )
    strategy._settled.add("g2")
    assert taken.settled == frozenset({"g1"}), "the value can be put back more than once"
