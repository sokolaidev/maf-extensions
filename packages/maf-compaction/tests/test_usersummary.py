"""Tests for the strategy that summarises the user's own turns.

Three things are checked harder than the rest, because three things are what this design is.

It must touch **only** user turns: the row exists to be compared with the tool-side rows in the
same table, and a strategy that quietly also shed tool results would make both numbers
unreadable.

It must **recompact its own output**, which is the opposite of what the anchored strategy does
with a result it has already shortened. That is the headline behaviour and the one a future
change is most likely to "fix" by refusing, so the test for it asserts the consequence -- the
conversation does not grow across two compactions -- rather than only the mechanics, because
the mechanics can be satisfied by a pass that inserts a second summary beside the first.

And it must **fail safe**: a summarizer that raises or answers with nothing has to leave a
conversation that is byte-identical to the one it was given, since the alternative is a band
excluded on the strength of a replacement that does not exist.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest
from agent_framework import CharacterEstimatorTokenizer, ChatResponse, Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
    project_included_messages,
)

from maf_compaction import _usersummary
from maf_compaction._preserve import (
    PRESERVE_REASON_KEY,
    PRESERVED_KEY,
    is_preserved,
    set_preserved,
)
from maf_compaction._usersummary import (
    DEFAULT_KEEP_HEAD_USER_TURNS,
    DEFAULT_KEEP_TAIL_USER_TURNS,
    DEFAULT_MIN_BAND_SHARE,
    DEFAULT_SUMMARY_MODE,
    DEFAULT_USER_FOLD_PROMPT,
    DEFAULT_USER_TRIGGER_FRACTION,
    EXCLUDE_REASON,
    FOLD_EXCLUDE_REASON,
    FOLD_ID_PREFIX,
    PRESERVE_REASON,
    SUMMARY_ID_PREFIX,
    SUMMARY_MODE_BOUNDARY,
    SUMMARY_MODE_FOLD,
    SUMMARY_MODE_RECOMPACT,
    SUMMARY_MODES,
    USER_SUMMARY_MARKER,
    UserTurnAnchoredSummarizationCompactionStrategy,
)

pytestmark = pytest.mark.anyio

TOKENIZER = CharacterEstimatorTokenizer()

#: Characters of filler in every user turn and every reply, so each message is about the same
#: size and the arithmetic below is exact rather than approximate. An eight-turn conversation
#: built from these measures roughly 16,700 tokens.
_TURN_CHARS = 4_000

#: A ceiling the eight-turn fixture is comfortably over, so the default trigger fires.
#:
#: 0.8 of this is 9,600 against a fixture of about 16,700, which is 174% of the line rather than
#: a value sitting near it. **Recompute both of these whenever a default moves**, and check the
#: margin rather than the sign: a fixture that slips under the trigger does not fail, it asserts
#: against a strategy that returned without doing anything, and passes.
_COMPACTING_CEILING = 12_000

#: A ceiling the same fixture is comfortably under, so nothing fires.
#:
#: 0.8 of this is 80,000 against the same 16,700, so the fixture is at 21% of the line.
_IDLE_CEILING = 100_000


class _Summarizer:
    """A summarizer that answers from a script, and records what it was asked.

    The requests are kept because what reaches the summarizer is part of the contract: it is
    sent the user's turns and nothing else, so a test can prove the assistant's replies and the
    tool output never leave the conversation.
    """

    def __init__(self, text: str = "The user asked for the earlier things, in order.") -> None:
        self.requests: list[list[Message]] = []
        self.text = text

    async def get_response(
        self, messages: list[Message], *, stream: bool = False, **kwargs: Any
    ) -> ChatResponse:
        self.requests.append(list(messages))
        return ChatResponse(messages=[Message(role="assistant", contents=[self.text])])


class _FailingSummarizer:
    """A summarizer that raises, which is one of the two ways this strategy must do nothing."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(
        self, messages: list[Message], *, stream: bool = False, **kwargs: Any
    ) -> ChatResponse:
        self.calls += 1
        raise RuntimeError("the summarizer is unavailable")


class _EmptySummarizer:
    """A summarizer that answers with whitespace, which is the other way."""

    async def get_response(
        self, messages: list[Message], *, stream: bool = False, **kwargs: Any
    ) -> ChatResponse:
        return ChatResponse(messages=[Message(role="assistant", contents=["   "])])


def _turn(index: int, *, chars: int = _TURN_CHARS) -> list[Message]:
    """Return one user turn and the assistant's reply to it.

    Args:
        index: Numbers the pair, so ids and text are unique and a test can name one of them.

    Keyword Args:
        chars: Filler in each message.

    Returns:
        The two messages.
    """
    return [
        Message(role="user", contents=[f"Turn {index}: " + "u" * chars], message_id=f"u{index}"),
        Message(
            role="assistant", contents=[f"Reply {index}: " + "a" * chars], message_id=f"a{index}"
        ),
    ]


def _tool_group(index: int) -> list[Message]:
    """Return a tool call and its result, which this strategy must never read or touch.

    Args:
        index: Numbers the pair.

    Returns:
        The two messages.
    """
    return [
        Message(
            role="assistant",
            contents=[
                {
                    "type": "function_call",
                    "call_id": f"call_{index}",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ],
            message_id=f"c{index}",
        ),
        Message(
            role="tool",
            contents=[
                {
                    "type": "function_result",
                    "call_id": f"call_{index}",
                    "result": f"TOOL-{index}-VALUE",
                }
            ],
            message_id=f"r{index}",
        ),
    ]


def _conversation(
    turns: int, *, first_turn: int = 0, tools_after: int | None = None
) -> list[Message]:
    """Return a conversation of ``turns`` user/assistant pairs.

    Args:
        turns: How many pairs to build.

    Keyword Args:
        first_turn: Index the pairs are numbered from, so a second stretch can be appended to
            the first without colliding on ids -- which is what a second crossing of the
            threshold needs.
        tools_after: Index of the turn a tool-call group follows, or None for a conversation
            with no tool work in it at all.

    Returns:
        The messages, opening with the system message a real conversation opens with.
    """
    messages: list[Message] = []
    if first_turn == 0:
        messages.append(
            Message(role="system", contents=["You are an assistant."], message_id="sys")
        )
    for offset in range(turns):
        index = first_turn + offset
        messages += _turn(index)
        if tools_after is not None and index == tools_after:
            messages += _tool_group(index)
    return messages


def _included(messages: list[Message]) -> int:
    """Return the token count the strategy itself would read off ``messages``.

    Annotates first, because a token count is cached per message and a conversation that has
    just been mutated carries numbers describing text that no longer exists.

    Args:
        messages: The conversation to measure.

    Returns:
        Included tokens.
    """
    annotate_message_groups(messages)
    annotate_token_counts(messages, tokenizer=TOKENIZER, force_retokenize=True)
    return included_token_count(messages)


def _rendered(messages: list[Message]) -> str:
    """Return what the model would be sent, excluded messages left out."""
    parts: list[str] = []
    for message in project_included_messages(messages):
        for content in message.contents:
            result = getattr(content, "result", None)
            text = getattr(content, "text", None)
            parts.append(
                str(result) if result is not None else (text if text is not None else str(content))
            )
    return "\n".join(parts)


def _user_texts(messages: list[Message]) -> list[str]:
    """Return the text of every user message still being sent, in order."""
    return [
        message.text or ""
        for message in project_included_messages(messages)
        if message.role == "user"
    ]


def _user_tokens(messages: list[Message]) -> int:
    """Return the tokens the user's own messages contribute to the prompt.

    The half this strategy owns, measured apart from the whole, because the other half grows
    with every turn whatever this does: the assistant's replies are not its to remove, so a
    total cannot say whether a compaction worked.
    """
    return sum(TOKENIZER.count_tokens(text) for text in _user_texts(messages))


def _strategy(
    summarizer: Any = None, **kwargs: Any
) -> UserTurnAnchoredSummarizationCompactionStrategy:
    """Return a strategy over the compacting ceiling unless a test says otherwise."""
    kwargs.setdefault("max_input_tokens", _COMPACTING_CEILING)
    return UserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=TOKENIZER, client=summarizer or _Summarizer(), **kwargs
    )


def test_the_fixture_sits_where_the_two_ceilings_assume_it_does() -> None:
    """A fixture that drifts under a trigger asserts against a strategy that did nothing.

    Every test here that expects a compaction is worthless if the conversation is below the
    line, and it does not fail when that happens -- it passes, having measured an early return.
    So the two margins are checked once, here, rather than being trusted to a comment that
    nothing reads.
    """
    size = _included(_conversation(8))

    assert size > _COMPACTING_CEILING * DEFAULT_USER_TRIGGER_FRACTION * 1.5, (
        "the fixture must clear the trigger"
    )
    assert size < _IDLE_CEILING * DEFAULT_USER_TRIGGER_FRACTION * 0.5, (
        "and must be nowhere near the idle one"
    )


def test_the_defaults_are_one_turn_at_each_end_a_late_trigger_and_a_tenth_of_the_prompt() -> None:
    """The four numbers a caller inherits, pinned where a reader of the table can find them.

    They are defaults rather than derivations, and a change to any of them changes what every
    archived row means, so moving one should have to move this line too. The band share is the
    one that changes how *often* the strategy acts rather than when: without it the strategy
    fires once per turn over the trigger.
    """
    assert (DEFAULT_KEEP_HEAD_USER_TURNS, DEFAULT_KEEP_TAIL_USER_TURNS) == (1, 1)
    assert DEFAULT_USER_TRIGGER_FRACTION == 0.8
    assert DEFAULT_MIN_BAND_SHARE == 0.1


def test_a_user_turn_quoting_the_marker_is_not_a_summary() -> None:
    """The marker identifies a summary only where the strategy writes it, at the start."""
    quoting = Message(role="user", contents=[f"Why does my log say {USER_SUMMARY_MARKER}?"])
    written = Message(role="user", contents=[f"{USER_SUMMARY_MARKER}\nThe user asked about X."])

    assert not _usersummary._is_summary(quoting)
    assert _usersummary._is_summary(written)


def test_a_user_turn_with_a_summary_like_id_is_not_a_summary() -> None:
    """A store may assign any id, so identity is read from the text alone."""
    turn = Message(role="user", contents=["An ordinary question."], message_id="user_summary_5")

    assert not _usersummary._is_summary(turn)


async def test_it_compacts_user_turns_and_leaves_tool_results_and_assistant_messages_alone() -> (
    None
):
    """The independence the whole comparison rests on.

    ``tool_summary_anchored`` and the anchored family act on the tool half; this acts on the
    user half; and the table reads the two as separate answers to separate questions. A
    strategy that shed both would make each row's ``snap%`` a sum of two effects with nothing
    saying how it divided -- and it would do it silently, because every assertion about *this*
    half would still pass.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer)
    messages = _conversation(8, tools_after=3)

    assert await strategy(messages) is True
    rendered = _rendered(messages)

    assert "TOOL-3-VALUE" in rendered, "the tool result is not this strategy's to remove"
    assert "Reply 4:" in rendered, "and neither is the assistant's narration"
    assert "Turn 4:" not in rendered, "while the user turn beside them is gone"
    untouched = [
        message
        for message in messages
        if message.role in {"assistant", "tool", "system"}
        and (message.additional_properties.get(EXCLUDED_KEY, False) or _summarized_by(message))
    ]
    assert not untouched, (
        f"these were annotated by a strategy that may only read user turns: {untouched}"
    )
    asked = summarizer.requests[0][-1].text or ""
    assert "Reply 4:" not in asked, "the summarizer is sent the user's turns and nothing else"
    assert "TOOL-3-VALUE" not in asked


async def test_the_first_and_last_user_turns_survive_on_the_defaults() -> None:
    """The task and the live request, which are the two turns that cannot be paraphrased.

    The first carries the requirements every surviving value is interpreted against -- the
    thing truncation measured itself losing, 29 of 53 facts left in a prompt the model could
    not use. The last is the question being asked right now, and a model answering a summary of
    it answers a different question.
    """
    strategy = _strategy()
    messages = _conversation(8)

    assert await strategy(messages) is True
    texts = _user_texts(messages)

    assert len(texts) == 3, f"head, one summary and tail, not {texts}"
    assert texts[0].startswith("Turn 0:")
    assert texts[1].startswith(USER_SUMMARY_MARKER)
    assert texts[2].startswith("Turn 7:")
    assert strategy.user_messages_replaced == 6


async def test_wider_anchors_keep_more_turns_verbatim_at_each_end() -> None:
    """Both ends move, and they move independently.

    One number for both would be the cheaper API and the wrong one: the two ends are protecting
    different things -- the terms of reference at the front, the live request at the back -- so
    the question "what is the opening task statement worth against the recent turns" can only
    be asked by moving them apart.
    """
    strategy = _strategy(keep_head_user_turns=2, keep_tail_user_turns=3)
    messages = _conversation(8)

    assert await strategy(messages) is True
    texts = _user_texts(messages)

    assert [text[:7] for text in texts] == [
        "Turn 0:",
        "Turn 1:",
        USER_SUMMARY_MARKER[:7],
        "Turn 5:",
        "Turn 6:",
        "Turn 7:",
    ]
    assert strategy.user_messages_replaced == 3


async def test_anchors_that_leave_no_band_compact_nothing() -> None:
    """A conversation shorter than its own anchors is a no-op, not an error or a summary of one.

    Asked for a head of four and a tail of four on eight turns, there is nothing in between,
    and the honest answer is to return False: a summary of an empty band would be a summarizer
    call spent on nothing and a message inserted that stands for no message at all.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, keep_head_user_turns=4, keep_tail_user_turns=4)
    messages = _conversation(8)
    before = list(messages)

    assert await strategy(messages) is False
    assert messages == before
    assert summarizer.requests == []
    assert strategy.user_compactions == 0


async def test_nothing_happens_below_the_trigger() -> None:
    """Under the line the strategy must not even look, let alone spend a summarizer call.

    A compaction that was not needed is the worst trade this package measures: it breaks the
    cached prefix, which is billed at the uncached rate for everything behind the edit, and
    saves tokens on a prompt that already fitted.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, max_input_tokens=_IDLE_CEILING)
    messages = _conversation(8)
    before = list(messages)

    assert await strategy(messages) is False
    assert messages == before, "an untriggered pass must not even annotate an exclusion"
    assert summarizer.requests == [], "and must not spend a call finding that out"
    assert (strategy.user_compactions, strategy.user_messages_replaced) == (0, 0)


async def test_a_second_crossing_recompacts_the_earlier_summary_together_with_the_new_turns() -> (
    None
):
    """The headline behaviour, and the one a later change is most likely to undo.

    ``AnchoredCompactionStrategy._shorten`` deliberately refuses to re-trim a result carrying
    its own removal marker, because its trigger is the band's geometry and re-trimming would
    fire on every pass. This strategy's trigger is a threshold that a compaction moves away
    from, so the same rewrite is rare by construction and re-reading its own summary is what
    stops summaries accumulating. Somebody who knows the first rule and not the second will
    make this strategy skip its own output, and every mechanical assertion below would still
    pass: the second pass would summarise the new turns alone and insert a second summary
    beside the first.

    So the consequence is asserted as well, and it is asserted on the user side of the prompt
    rather than on the whole of it: the assistant's replies are not this strategy's to remove
    and they grow with every turn, so a total would rise across two compactions however well
    the user band was compacted. On the half this strategy owns the arithmetic is exact. With a
    summarizer of fixed size and turns of one size, a conversation that recompacts carries the
    same user-side tokens after the second pass as after the first -- head, one summary, tail --
    while one that refuses carries a whole extra summary, and another on every crossing after
    that.
    """
    strategy = _strategy()
    messages = _conversation(8)

    assert await strategy(messages) is True
    user_after_first = _user_tokens(messages)
    first_summary = next(
        message for message in messages if (message.message_id or "").startswith(SUMMARY_ID_PREFIX)
    )

    messages += _conversation(6, first_turn=8)
    before_second = _included(messages)
    user_before_second = _user_tokens(messages)

    assert await strategy(messages) is True
    after_second = _included(messages)
    user_after_second = _user_tokens(messages)
    summaries = [message for message in project_included_messages(messages) if _is_summary(message)]

    assert strategy.user_compactions == 2
    assert len(summaries) == 1, (
        f"the earlier summary must be replaced, not joined: {len(summaries)} are being sent"
    )
    assert summaries[0] is not first_summary
    assert first_summary.additional_properties[EXCLUDED_KEY] is True, (
        "the earlier summary was superseded"
    )
    assert first_summary.message_id in _summary_of_message_ids(summaries[0]), (
        "and the new summary has to say so, or nothing records that it stands for the old one"
    )
    assert after_second < before_second, "the second pass has to make the conversation smaller"
    assert user_after_second < user_before_second
    assert len(_user_texts(messages)) == 3, (
        "head, one summary and tail, exactly as after the first pass"
    )
    assert user_after_second <= user_after_first, (
        "a pass that refused to re-read its own summary would leave the user side of the "
        "prompt larger after the second compaction than after the first, by one whole summary"
    )
    # Seven new turns' worth of material -- the six appended plus the summary standing for the
    # first six -- against one message, which is the count a refusal could not produce.
    assert strategy.user_messages_replaced == 7


async def test_a_band_holding_only_its_own_summary_is_left_alone() -> None:
    """Recompaction is licensed by new material, not by the prompt still being large.

    The size that fired the trigger does not go away when the compaction that answered it is
    already in the prompt, so a pass reading size alone would rewrite the same message at the
    same position on every turn for the rest of the run -- which is precisely the thrash
    ``_shorten`` refuses to do, arrived at from the other direction. The middleware in
    ``_toolsummary`` had the same defect and ``_record_due`` is where it was fixed.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer)
    messages = _conversation(8)

    assert await strategy(messages) is True
    assert len(summarizer.requests) == 1

    assert await strategy(messages) is False, (
        "nothing new has been said, so there is nothing to recompact"
    )
    assert len(summarizer.requests) == 1, "and no call may be spent discovering that"
    assert strategy.user_compactions == 1


async def test_a_summarizer_failure_leaves_the_conversation_untouched_and_is_counted() -> None:
    """No replacement means no supersession, and the row has to say the pass was lost.

    Excluding the band first and restoring it on failure is the obvious alternative and is the
    mutate-and-roll-back ``MinimumGainAnchoredCompactionStrategy`` already refused, on the
    ground that a restore missing one field is a silent wrong answer. Here it would be worse
    than silent: the band would be gone and nothing would stand in its place.
    """
    summarizer = _FailingSummarizer()
    strategy = _strategy(summarizer)
    messages = _conversation(8)
    before = list(messages)

    assert await strategy(messages) is False
    assert messages == before
    assert summarizer.calls == 1, "the failure has to be a real attempt, not a refusal to try"
    assert strategy.user_summary_failures == 1
    assert (strategy.user_compactions, strategy.user_messages_replaced) == (0, 0)


async def test_a_summarizer_that_answers_with_nothing_is_the_same_failure() -> None:
    """An empty summary and a raised exception leave the caller with the same thing: no text.

    Treated apart, the empty case is the one that gets through -- it is not an error, so it
    reads as success, and the band would be superseded by a message that says nothing at all.
    """
    strategy = _strategy(_EmptySummarizer())
    messages = _conversation(8)
    before = list(messages)

    assert await strategy(messages) is False
    assert messages == before
    assert strategy.user_summary_failures == 1


async def test_the_replacement_carries_the_frameworks_own_supersession_annotations() -> None:
    """Replace, in the way the framework already means by it, rather than in a private way.

    ``SummarizationStrategy`` links a summary to what it replaced in both directions and
    excludes the originals with a reason, and the framework's own summary reconciliation reads
    those annotations. A strategy that dropped the band instead, or linked it with keys of its
    own, would produce a conversation that is only legible to itself.
    """
    strategy = _strategy()
    messages = _conversation(8)

    assert await strategy(messages) is True
    summary = next(
        message for message in messages if (message.message_id or "").startswith(SUMMARY_ID_PREFIX)
    )
    replaced = [
        message
        for message in messages
        if message.message_id in {f"u{index}" for index in range(1, 7)}
    ]

    assert _summary_of_message_ids(summary) == [f"u{index}" for index in range(1, 7)]
    assert len(_summary_of_group_ids(summary)) == 6
    for message in replaced:
        assert message.additional_properties[EXCLUDED_KEY] is True
        assert message.additional_properties[EXCLUDE_REASON_KEY] == EXCLUDE_REASON
        assert _summarized_by(message) == summary.message_id
    assert messages.index(summary) < messages.index(replaced[0]), (
        "the summary stands where the band started"
    )


async def test_the_replacement_is_a_user_message() -> None:
    """It stands for user turns, and only a user message can be recompacted as one.

    The framework's summarizer writes an assistant message because it summarises groups of
    every kind. Here that choice would make the summary invisible to this strategy's own
    selection rule, so the first summary would sit in the prompt untouched for the rest of the
    run -- and it would put two assistant messages in a row wherever the band ended just before
    a reply, which several providers reject.
    """
    strategy = _strategy()
    messages = _conversation(8)

    assert await strategy(messages) is True
    summary = next(
        message for message in messages if (message.message_id or "").startswith(SUMMARY_ID_PREFIX)
    )

    assert summary.role == "user"
    assert USER_SUMMARY_MARKER in (summary.text or ""), (
        "a model shown a silently reduced conversation answers as though it had seen all of it"
    )


async def test_a_preserved_user_turn_is_never_summarised() -> None:
    """``_preserve`` means no strategy may shorten, drop or shed a message, and this drops.

    The mark exists so one strategy cannot destroy another's output. Superseding
    a preserved turn is the same loss by a politer route: the message stops being sent, and
    what stands in its place is a paraphrase written by a different model.
    """
    strategy = _strategy()
    messages = _conversation(8)
    protected = next(message for message in messages if message.message_id == "u3")
    set_preserved(protected, preserved=True, reason="a test")

    assert await strategy(messages) is True

    assert protected.additional_properties.get(EXCLUDED_KEY, False) is False
    assert "Turn 3:" in _rendered(messages)
    assert strategy.user_messages_replaced == 5


async def test_a_preserved_head_turn_stays_the_head_and_the_summary_is_still_recompacted() -> None:
    """The anchors are positions in the prompt, and a mark another party sets does not move them.

    Counting the head over the unprotected turns only would land it on the head's neighbour once
    the head is preserved -- and after a first pass in the recompacting mode that neighbour is
    the strategy's own summary, which would then be held as the head and never re-read, leaving
    two standing summaries in the one mode whose point is that it carries one. So the case is driven for two more passes, and what is asserted is the
    mode's own promise: one standing summary, the earlier one superseded by it, with the head
    turn verbatim and unexcluded throughout and the tail still the tail.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer)
    messages = _conversation(8)
    assert await strategy(messages) is True
    head = next(message for message in messages if message.message_id == "u0")
    set_preserved(head, preserved=True, reason="another party")

    for start in (8, 14):
        messages += _conversation(6, first_turn=start)
        (earlier,) = _standing(messages)
        assert await strategy(messages) is True
        asked = summarizer.requests[-1][-1].text or ""
        (standing,) = _standing(messages)

        assert standing is not earlier, "one summary stands, and it is the new one"
        assert earlier.additional_properties[EXCLUDED_KEY] is True, (
            "the earlier one was recompacted, not held as the head"
        )
        assert earlier.message_id in _summary_of_message_ids(standing)
        assert USER_SUMMARY_MARKER in asked, "the earlier summary was re-read"
        assert "Turn 0:" not in asked
        assert head.additional_properties.get(EXCLUDED_KEY, False) is False
        assert f"Turn {start + 5}:" not in asked, "the tail is still the tail"
    assert _user_texts(messages)[0].startswith("Turn 0:")
    assert strategy.user_messages_replaced == 7, (
        "the summary and six new turns, exactly as with an unmarked head"
    )


async def test_a_preserved_last_turn_is_the_tail_and_the_turn_before_it_is_in_the_band() -> None:
    """The same rule at the other anchor, where the old arithmetic protected a turn too many.

    A preserved last turn is the live request, which the tail keeps whether or not it is marked;
    dropping it from the count made the turn before it the tail instead, and that turn has
    already been answered.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer)
    messages = _conversation(8)
    last = next(message for message in messages if message.message_id == "u7")
    set_preserved(last, preserved=True, reason="another party")

    assert await strategy(messages) is True
    asked = summarizer.requests[0][-1].text or ""

    assert "Turn 6:" in asked, "the turn before a preserved tail is in the band"
    assert "Turn 7:" not in asked
    assert last.additional_properties.get(EXCLUDED_KEY, False) is False
    assert strategy.user_messages_replaced == 6


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param({"max_input_tokens": 0}, "max_input_tokens", id="ceiling"),
        pytest.param({"keep_head_user_turns": -1}, "keep_head_user_turns", id="head"),
        pytest.param({"keep_tail_user_turns": -1}, "keep_tail_user_turns", id="tail"),
        pytest.param({"trigger_fraction": 0.0}, "trigger_fraction", id="trigger-zero"),
        pytest.param({"trigger_fraction": 1.5}, "trigger_fraction", id="trigger-above-one"),
        pytest.param({"min_band_share": -0.1}, "min_band_share", id="share-negative"),
        pytest.param({"min_band_share": 1.0}, "min_band_share", id="share-one"),
    ],
)
def test_a_configuration_outside_its_range_is_refused_by_the_constructor(
    kwargs: dict[str, Any], match: str
) -> None:
    """The bound belongs here, where the class is, and not in whatever is building it.

    ``compaction/`` ships without the benchmark, so a check written in the lab's pre-flight
    would leave the class unguarded for every other caller -- and would give a sweep two places
    to disagree about what is legal. The lab's job is only to build every selected strategy
    before a run spends anything, so that these errors arrive at the command line.
    """
    with pytest.raises(ValueError, match=match):
        _strategy(**kwargs)


#: Characters in a user turn of the lopsided fixture, against ``_FAT_REPLY_CHARS`` in its reply.
#:
#: 1,000 against 4,000, so the user half is a fifth of the conversation and the band between the
#: anchors about a fifth of the prompt. That ratio is the whole point of the fixture: the
#: even-sized conversation above hands this strategy half the prompt, where one pass takes the
#: prompt so far under the trigger that nothing else can be measured, while a live conversation
#: can have its band at 28% of the prompt and the rest in assistant replies and tool payload the
#: strategy may not touch.
_THIN_USER_CHARS = 1_000

#: Characters in an assistant reply of the lopsided fixture. See ``_THIN_USER_CHARS``.
_FAT_REPLY_CHARS = 4_000


def _lopsided_turn(index: int) -> list[Message]:
    """Return one user turn and a reply four times its size.

    Args:
        index: Numbers the pair.

    Returns:
        The two messages.
    """
    return [
        Message(
            role="user",
            contents=[f"Turn {index}: " + "u" * _THIN_USER_CHARS],
            message_id=f"u{index}",
        ),
        Message(
            role="assistant",
            contents=[f"Reply {index}: " + "a" * _FAT_REPLY_CHARS],
            message_id=f"a{index}",
        ),
    ]


async def _grow_and_compact(
    strategy: UserTurnAnchoredSummarizationCompactionStrategy, turns: int
) -> list[tuple[int, int, bool]]:
    """Run one pass per turn over a conversation that never stops growing.

    This is the shape a live run has: the strategy is called once per agent turn on a
    conversation one turn longer each time, rather than once on a conversation that is already
    long. A rule can be right for one pass and wrong for the hundredth, so the driver is written
    out once and shared.

    Args:
        strategy: The strategy under test, called once per turn.
        turns: How many turns to seed.

    Returns:
        One ``(turn, prompt tokens before the pass, whether it compacted)`` per turn.
    """
    messages = [Message(role="system", contents=["You are an assistant."], message_id="sys")]
    passes: list[tuple[int, int, bool]] = []
    for index in range(turns):
        messages += _lopsided_turn(index)
        before = _included(messages)
        passes.append((index, before, await strategy(messages)))
    return passes


async def test_a_conversation_that_keeps_growing_compacts_a_bounded_number_of_times() -> None:
    """Past the trigger the strategy must not compact on every turn.

    Past the trigger the prompt does not shrink to the size of the band, so the trigger stays
    true, and every new turn satisfies the "something here is not my own summary" rule. Only the
    band share stops a pass per turn, each of which breaks the cached prefix.

    Both arms below run the same forty-turn conversation one turn at a time, and differ in one
    number. The bounded arm is asserted against the unbounded one rather than against a
    constant, because the constant is what a future change would quietly re-tune.
    """
    hysteretic = _strategy()
    unbounded = _strategy(min_band_share=0.0)

    bounded_passes = await _grow_and_compact(hysteretic, 40)
    await _grow_and_compact(unbounded, 40)

    line = _COMPACTING_CEILING * DEFAULT_USER_TRIGGER_FRACTION
    over_the_line = [index for index, before, _ in bounded_passes if before > line]
    assert len(over_the_line) > 20, (
        "the fixture has to spend most of the run over the trigger or this measures nothing"
    )
    assert unbounded.user_compactions >= 25, (
        "without the share the strategy fires on very nearly every pass over the trigger, which "
        "is what this test exists to keep out"
    )
    assert 2 <= hysteretic.user_compactions <= 8, (
        f"bounded and small, and neither once per turn nor never: {hysteretic.user_compactions} "
        f"against {unbounded.user_compactions} without the share"
    )
    assert hysteretic.user_passes_declined >= 15, (
        "and every pass it did not spend is counted rather than silent"
    )


async def test_the_prompt_has_to_grow_between_two_compactions_by_the_factor_the_bound_claims() -> (
    None
):
    """The bound itself, rather than the small number it produces on one fixture.

    ``DEFAULT_MIN_BAND_SHARE`` claims a geometric bound: a band regrows only from the user turns
    added since the last pass, so for the band to be worth ``f`` of the prompt again the prompt
    must have grown by at least ``1 / (1 - f)``. That is what makes the firing count logarithmic
    in the length of the conversation rather than linear, and it is the claim a reader of the
    module has to be able to trust -- a test pinning only "eight or fewer on this fixture" would
    still pass if the mechanism became a per-turn counter that happened to divide by eight.
    """
    strategy = _strategy()

    passes = await _grow_and_compact(strategy, 40)
    fired_at = [before for _, before, fired in passes if fired]

    assert len(fired_at) >= 2, "one compaction cannot show a ratio between two"
    ratios = [later / earlier for earlier, later in zip(fired_at, fired_at[1:], strict=False)]
    assert all(ratio >= 1 / (1 - DEFAULT_MIN_BAND_SHARE) for ratio in ratios), (
        f"consecutive compactions must be a factor 1/(1-f) apart in prompt size, and these are {ratios}"
    )


async def test_a_share_of_zero_is_the_behaviour_every_archived_row_was_measured_with() -> None:
    """A share of zero runs without hysteresis, because the comparison needs it.

    A ``user_summary_anchored`` row recorded without a band share reads the setting back as
    ``0.0``. If zero did not reproduce that behaviour the reading would be a lie, and the A/B
    that justifies the default could not be run at all.
    """
    strategy = _strategy(min_band_share=0.0)
    messages = _conversation(8)

    assert await strategy(messages) is True
    assert strategy.user_compactions == 1

    messages += _conversation(1, first_turn=8)

    assert await strategy(messages) is True, (
        "one new turn is enough to recompact when nothing bounds it"
    )
    assert (strategy.user_compactions, strategy.user_messages_replaced) == (2, 2), (
        "the summary and the one turn after it, which is USERREPLACED:2 -- the live signature"
    )
    assert strategy.user_passes_declined == 0


async def test_a_band_worth_less_than_the_share_is_declined_before_the_summarizer_is_called() -> (
    None
):
    """The rule at its own boundary, on a band that is new material rather than an old summary.

    The "nothing but my own summary" rule cannot catch this: everything in this band is a turn
    the strategy has never seen. What makes the pass not worth making is size alone -- one turn
    against a prompt made of assistant replies -- and before the share existed this was a
    summarizer call and a rewritten prefix spent to remove a fraction of a percent of the prompt.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, keep_head_user_turns=3, keep_tail_user_turns=4)
    messages = _conversation(8)

    assert await strategy(messages) is False, "one turn between the anchors is not worth a pass"
    assert (strategy.user_compactions, strategy.user_passes_declined) == (0, 1)
    assert summarizer.requests == [], "declined before the call, which is where the saving is"

    generous = _strategy(
        _Summarizer(), keep_head_user_turns=3, keep_tail_user_turns=4, min_band_share=0.0
    )

    assert await generous(_conversation(8)) is True, (
        "and the same band is compacted when nothing bounds it"
    )


async def test_the_share_is_measured_against_the_prompt_and_not_against_the_ceiling() -> None:
    """Which denominator it is decides whether the bound is geometric or linear.

    Against the ceiling the bar would be a fixed number of tokens, the band would clear it again
    after a fixed amount of growth, and the firing count would rise linearly with the length of
    the conversation -- the same defect one order of magnitude quieter. Against the prompt the
    bar rises as the prompt does, which is what makes each pass need proportionally more
    material than the last.

    Asserted by holding the band fixed and moving everything else: the same eight user turns are
    worth a pass in a conversation of their own size and not worth one in a conversation several
    times the size, on one ceiling and with one band.
    """
    small = _strategy()
    large = _strategy()
    thin: list[Message] = [
        Message(role="system", contents=["You are an assistant."], message_id="sys")
    ]
    fat: list[Message] = [
        Message(role="system", contents=["You are an assistant."], message_id="sys")
    ]
    for index in range(8):
        thin += _turn(index)
        fat += [
            Message(
                role="user",
                contents=[f"Turn {index}: " + "u" * _TURN_CHARS],
                message_id=f"u{index}",
            ),
            Message(
                role="assistant",
                contents=[f"Reply {index}: " + "a" * _TURN_CHARS * 9],
                message_id=f"a{index}",
            ),
        ]

    assert _user_tokens(fat) == _user_tokens(thin), (
        "one band, so the only thing moving is the prompt around it"
    )
    assert _included(fat) > _included(thin) * 4

    assert await small(thin) is True
    assert await large(fat) is False
    assert (large.user_compactions, large.user_passes_declined) == (0, 1)


async def test_the_four_outcomes_of_a_pass_partition_the_passes() -> None:
    """A row that did nothing has exactly one number saying why, and the numbers add up.

    Four counters is two more than this class had, and the reason for each is that
    ``user_compactions == 0`` had four readings and no way to tell them apart: the conversation
    never grew, the band was not worth a pass, the summarizer failed, or -- on a composed row --
    the phase in front took the prompt below the line. A partition is the strongest form of that
    claim: every pass over a non-empty conversation lands in exactly one of the four, so an
    outcome added later without a counter makes this fail.
    """
    strategy = _strategy()
    passes = await _grow_and_compact(strategy, 40)
    failing = _strategy(_FailingSummarizer())
    failing_passes = await _grow_and_compact(failing, 40)

    for name, subject, count in (
        ("clean", strategy, len(passes)),
        ("failing", failing, len(failing_passes)),
    ):
        assert (
            subject.user_compactions
            + subject.user_passes_declined
            + subject.user_passes_below_trigger
            + subject.user_summary_failures
            + subject.user_summaries_replayed
            == count
        ), f"the {name} run's counters have to account for every pass, and account for it once"

    assert strategy.user_passes_below_trigger > 0, "the early turns are under the trigger"
    assert strategy.user_summary_failures == 0
    assert failing.user_compactions == 0, "the failing arm never replaced anything"
    assert failing.user_summary_failures > 0, (
        "and says so rather than reading as a band nobody wanted"
    )


#: Characters in a user turn of the user-heavy fixture, against ``_LIGHT_REPLY_CHARS`` in its reply.
#:
#: Eight to one, the reverse of the lopsided fixture above, and for the opposite reason. The
#: three modes differ only in what they do with the summaries they leave behind, so they come
#: apart only on a conversation where those summaries are a share of the prompt worth arguing
#: over. On the lopsided fixture the standing summaries are 0.35% of the prompt and the three
#: modes are three passes each, indistinguishable at a glance; here the user half is most of
#: the prompt, and a summarizer that keeps a stated fraction of what it reads leaves summaries
#: large enough for the boundary mode's floor to be visible and the fold's threshold to be met.
_HEAVY_USER_CHARS = 4_000

#: Characters in an assistant reply of the user-heavy fixture. See ``_HEAVY_USER_CHARS``.
_LIGHT_REPLY_CHARS = 500

#: Characters of a fixed, large summary: about 750 tokens under the character estimator.
#:
#: Large enough that three of them are a measurable share of the eight-turn fixture, which is
#: what a test of the fold's threshold needs, and fixed so that the share can be computed from
#: the conversation and asserted as the test's premise.
_LARGE_SUMMARY_CHARS = 3_000


class _RatioSummarizer:
    """A summarizer whose answer is a stated fraction of what it was asked to summarise.

    The stub summarizer above answers with one fixed sentence, which is right for every test
    about *which* turns are replaced and wrong for every test about what a summary costs: a
    fixed twelve-token summary makes the standing summaries a rounding error of the prompt in
    every mode, so the whole difference between the modes -- what the floor is worth, and
    whether a fold repays its break -- never shows. A fraction is the shape a real summarizer
    has: it keeps some of what it reads, and a fold of summaries keeps some of that.
    """

    def __init__(self, ratio: float) -> None:
        self.ratio = ratio
        self.requests: list[list[Message]] = []

    async def get_response(
        self, messages: list[Message], *, stream: bool = False, **kwargs: Any
    ) -> ChatResponse:
        self.requests.append(list(messages))
        body = messages[-1].text or ""
        return ChatResponse(
            messages=[Message(role="assistant", contents=["s" * int(len(body) * self.ratio)])]
        )


class _NumberedSummarizer:
    """A summarizer whose every answer is different, so a rewrite is visible as bytes.

    The fixed-sentence stub would make a replaced summary render identically to the one it
    replaced, and a test of whether the cached prefix survived a pass would then pass on a
    strategy that rewrote it. A real summarizer never answers twice alike.
    """

    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    async def get_response(
        self, messages: list[Message], *, stream: bool = False, **kwargs: Any
    ) -> ChatResponse:
        self.requests.append(list(messages))
        return ChatResponse(
            messages=[Message(role="assistant", contents=[f"Summary number {len(self.requests)}."])]
        )


def _heavy_turn(index: int) -> list[Message]:
    """Return one user turn and a reply an eighth of its size. See ``_HEAVY_USER_CHARS``."""
    return [
        Message(
            role="user",
            contents=[f"Turn {index}: " + "u" * _HEAVY_USER_CHARS],
            message_id=f"u{index}",
        ),
        Message(
            role="assistant",
            contents=[f"Reply {index}: " + "a" * _LIGHT_REPLY_CHARS],
            message_id=f"a{index}",
        ),
    ]


def _tiny_turn(index: int) -> list[Message]:
    """Return one turn too small to move anything: the slow growth a fold must not thrash on."""
    return [
        Message(role="user", contents=[f"Turn {index}: " + "u" * 40], message_id=f"u{index}"),
        Message(role="assistant", contents=[f"Reply {index}: " + "a" * 40], message_id=f"a{index}"),
    ]


def _standing(messages: list[Message]) -> list[Message]:
    """Return the strategy's summaries still being sent, by the test's own recognition rule."""
    return [
        message
        for message in project_included_messages(messages)
        if message.role == "user" and _is_summary(message)
    ]


def _rendered_messages(messages: list[Message]) -> list[str]:
    """Return what the model would be sent, one string per included message, in order."""
    rendered: list[str] = []
    for message in project_included_messages(messages):
        parts: list[str] = []
        for content in message.contents:
            result = getattr(content, "result", None)
            text = getattr(content, "text", None)
            parts.append(
                str(result) if result is not None else (text if text is not None else str(content))
            )
        rendered.append("\n".join(parts))
    return rendered


def _outcome_of(
    strategy: UserTurnAnchoredSummarizationCompactionStrategy, before: tuple[int, ...]
) -> str:
    """Name which of the five counters a pass moved, given the four it could have moved."""
    after = (
        strategy.user_compactions,
        strategy.user_folds,
        strategy.user_passes_declined,
        strategy.user_passes_below_trigger,
    )
    names = ("compacted", "folded", "declined", "under")
    moved = [name for name, was, now in zip(names, before, after, strict=True) if now > was]
    return moved[0] if moved else "failed"


async def _grow_in_mode(
    strategy: UserTurnAnchoredSummarizationCompactionStrategy,
    turns: int,
    *,
    turn: Any = _heavy_turn,
    messages: list[Message] | None = None,
    first_turn: int = 0,
) -> tuple[list[Message], list[tuple[int, int, str]]]:
    """Run one pass per turn, recording what each pass did and how many summaries stood before it.

    The same driver as ``_grow_and_compact`` with the outcome named per pass, because the fold's
    two guards are statements about consecutive passes: a fold may not follow a pass that left
    fewer than two summaries standing, and that is only checkable pass by pass.

    Args:
        strategy: The strategy under test.
        turns: How many turns to append.

    Keyword Args:
        turn: Builds one turn from its index.
        messages: A conversation to continue, or None to start one.
        first_turn: Index the appended turns are numbered from.

    Returns:
        The conversation, and one ``(turn, summaries standing before the pass, outcome)`` per turn.
    """
    conversation: list[Message] = (
        [Message(role="system", contents=["You are an assistant."], message_id="sys")]
        if messages is None
        else messages
    )
    events: list[tuple[int, int, str]] = []
    for offset in range(turns):
        index = first_turn + offset
        conversation += turn(index)
        standing_before = len(_standing(conversation))
        before = (
            strategy.user_compactions,
            strategy.user_folds,
            strategy.user_passes_declined,
            strategy.user_passes_below_trigger,
        )
        await strategy(conversation)
        events.append((index, standing_before, _outcome_of(strategy, before)))
    return conversation, events


def test_the_default_mode_is_the_recompacting_one_until_a_run_has_measured_the_arms() -> None:
    """The default does not move until a run has measured both arms, which is this package's rule.

    Flipping the default before then would invalidate the comparison the boundary mode exists to
    be measured in. So the default is pinned, and pinned to the *literal* rather than to whichever mode reads best in
    the module docstring, because reading best is not the same as having been measured.
    """
    assert DEFAULT_SUMMARY_MODE == SUMMARY_MODE_RECOMPACT
    assert SUMMARY_MODES == (SUMMARY_MODE_RECOMPACT, SUMMARY_MODE_BOUNDARY, SUMMARY_MODE_FOLD)
    assert _strategy().summary_mode == SUMMARY_MODE_RECOMPACT
    assert _strategy().recompacts_summaries is True
    with pytest.raises(ValueError, match="summary_mode"):
        _strategy(summary_mode="boundaries")


async def test_a_standing_summary_is_neither_re_read_nor_replaced_by_a_later_pass() -> None:
    """The boundary mode's whole promise, asserted on the object rather than on a count.

    The recompacting test above proves its mode by the earlier summary being excluded and the
    new one standing for it. This is the same two facts negated, and it has to be the *same*
    object: a pass that quietly replaced the boundary with an identical copy would satisfy any
    count and break the cache all the same.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, summary_mode=SUMMARY_MODE_BOUNDARY)
    messages = _conversation(8)

    assert await strategy(messages) is True
    (first,) = _standing(messages)

    messages += _conversation(6, first_turn=8)

    assert await strategy(messages) is True
    standing = _standing(messages)

    assert len(standing) == 2, "the earlier summary stands beside the new one"
    assert standing[0] is first, "and it is the same object, not a copy at the same position"
    assert first.additional_properties.get(EXCLUDED_KEY, False) is False
    assert _summarized_by(first) is None, "nothing stands for it"
    assert first.message_id not in _summary_of_message_ids(standing[1]), (
        "and the new summary does not claim to"
    )
    asked = summarizer.requests[1][-1].text or ""
    assert USER_SUMMARY_MARKER not in asked, "the boundary was not sent to the summarizer"
    assert strategy.user_compactions == 2
    assert strategy.user_messages_replaced == 6, (
        "Turn 7, the old tail, through Turn 12 -- and not the summary"
    )


async def test_the_band_after_a_boundary_holds_only_turns_newer_than_it() -> None:
    """Where the second pass's band begins is the rule, and the summarizer's request is the proof.

    What reaches the summarizer is exactly the band, so the request is read directly: every
    turn newer than the boundary but the tail, no turn older than it, and not the boundary.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, summary_mode=SUMMARY_MODE_BOUNDARY)
    messages = _conversation(8)
    assert await strategy(messages) is True
    messages += _conversation(6, first_turn=8)

    assert await strategy(messages) is True
    asked = summarizer.requests[1][-1].text or ""
    superseded = [
        message.message_id
        for message in messages
        if message.role == "user"
        and message.additional_properties.get(EXCLUDE_REASON_KEY) == EXCLUDE_REASON
    ]

    assert all(f"Turn {index}:" in asked for index in range(7, 13)), (
        "Turn 7 through Turn 12 are the band"
    )
    assert "Turn 13:" not in asked, "the tail is kept"
    assert not any(f"Turn {index}:" in asked for index in range(7)), (
        "nothing older than the boundary is re-read"
    )
    assert superseded == [f"u{index}" for index in range(1, 13)]


async def test_n_passes_leave_n_standing_summaries_in_the_boundary_modes_and_one_when_recompacting() -> (
    None
):
    """The floor, counted on the conversation and on the counters, in all three modes.

    Three crossings of the eight-turn fixture with the fixed-sentence summarizer: 54 tokens of
    summary standing after the recompacting run, 162 after the boundary run, and the prompt
    108 tokens larger for it -- 22,755 against 22,863 under the character estimator. Small,
    because the stub's summaries are small; the user-heavy test below is where it is not. The
    fold mode leaves three as well here, because three twelve-token summaries are nowhere near
    a tenth of the prompt, which is the fold's threshold doing its job.
    """
    left: dict[str, tuple[int, int, int]] = {}
    for mode in SUMMARY_MODES:
        strategy = _strategy(summary_mode=mode)
        messages = _conversation(8)
        assert await strategy(messages) is True
        for start in (8, 14):
            messages += _conversation(6, first_turn=start)
            assert await strategy(messages) is True
        standing = _standing(messages)
        assert strategy.user_compactions == 3
        assert strategy.user_summaries_in_conversation == len(standing), (
            "the counter is the conversation"
        )
        assert strategy.user_summary_tokens == included_token_count(standing), (
            "and so are the tokens"
        )
        assert strategy.user_folds == 0
        left[mode] = (len(standing), strategy.user_summary_tokens, _included(messages))

    assert left[SUMMARY_MODE_RECOMPACT][0] == 1, "one message stands for everything behind it"
    assert left[SUMMARY_MODE_BOUNDARY][0] == 3, "one summary per pass, and none replaced"
    assert left[SUMMARY_MODE_FOLD][0] == 3, (
        "no fold while the summaries are a rounding error of the prompt"
    )
    assert left[SUMMARY_MODE_BOUNDARY][1] > left[SUMMARY_MODE_RECOMPACT][1]
    assert left[SUMMARY_MODE_BOUNDARY][2] > left[SUMMARY_MODE_RECOMPACT][2], (
        "the floor is on the prompt"
    )


async def test_the_prefix_up_to_the_newest_boundary_is_byte_identical_across_a_later_pass() -> None:
    """The cache claim, and the one assertion worth making directly.

    Prompt caching is strict-prefix, so what the boundary mode buys is precisely this: every
    message up to and including the newest boundary renders to the same bytes, in the same
    order, before and after a later pass, and they are the same objects. The recompacting mode
    is asserted in the same test to break that prefix at the summary's position -- with a
    summarizer whose answers differ, because the fixed-sentence stub would let a rewrite render
    identically and pass a test it should fail.
    """
    outcomes: dict[str, tuple[bool, bool]] = {}
    for mode in (SUMMARY_MODE_RECOMPACT, SUMMARY_MODE_BOUNDARY):
        strategy = _strategy(_NumberedSummarizer(), summary_mode=mode)
        messages = _conversation(8)
        assert await strategy(messages) is True
        projected = project_included_messages(messages)
        boundary_at = next(index for index, message in enumerate(projected) if _is_summary(message))
        prefix_objects = projected[: boundary_at + 1]
        prefix_bytes = _rendered_messages(messages)[: boundary_at + 1]

        messages += _conversation(6, first_turn=8)
        assert await strategy(messages) is True

        after_objects = project_included_messages(messages)[: boundary_at + 1]
        same_bytes = _rendered_messages(messages)[: boundary_at + 1] == prefix_bytes
        same_objects = all(a is b for a, b in zip(after_objects, prefix_objects, strict=False))
        outcomes[mode] = (same_bytes, same_objects)

    assert outcomes[SUMMARY_MODE_BOUNDARY] == (True, True), (
        "the prefix through the boundary is untouched"
    )
    assert outcomes[SUMMARY_MODE_RECOMPACT] == (False, False), (
        "and the recompacting mode rewrites it at the summary's position, which is the "
        "strict-prefix break the boundary mode exists to avoid"
    )


async def test_the_first_pass_in_the_boundary_mode_honours_the_head() -> None:
    """No boundary yet means the ordinary band, head and all.

    The boundary rule replaces the head only once there is a boundary to start from. On the
    first pass there is none, and a first pass that started from the beginning of the
    conversation would summarise the task statement -- the one turn every deleting strategy was
    measured losing first.
    """
    strategy = _strategy(keep_head_user_turns=2, summary_mode=SUMMARY_MODE_BOUNDARY)
    messages = _conversation(8)

    assert await strategy(messages) is True

    assert [text[:7] for text in _user_texts(messages)] == [
        "Turn 0:",
        "Turn 1:",
        USER_SUMMARY_MARKER[:7],
        "Turn 7:",
    ]
    assert strategy.user_summaries_in_conversation == 1


async def test_a_boundary_is_preserved_under_its_own_reason_and_re_marked_on_every_pass() -> None:
    """The protection is the subpackage's one mark, applied every pass, and only in the boundary modes.

    ``PRESERVED_KEY`` is what every removal path in ``_anchored`` and ``_toolsummary`` already
    honours, so marking the boundary is what makes "the record half cannot reach a user group"
    a contract rather than an accident of group kinds. Re-applied every pass because the mark is
    not promised to survive a reload, as ``_preserve_records`` re-applies it to the record. And
    *not* applied in the recompacting mode: ``_band`` skips a preserved turn, so a marked summary
    could never be recompacted and that mode would silently have become this one.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, summary_mode=SUMMARY_MODE_BOUNDARY)
    messages = _conversation(8)
    assert await strategy(messages) is True
    (summary,) = _standing(messages)

    assert is_preserved(summary)
    assert (
        summary.additional_properties[PRESERVE_REASON_KEY]
        == PRESERVE_REASON
        == "user_summary_boundary"
    )

    del summary.additional_properties[PRESERVED_KEY]
    del summary.additional_properties[PRESERVE_REASON_KEY]
    assert await strategy(messages) is False, "nothing new, so the pass declines"
    assert len(summarizer.requests) == 1, "and did not re-read the boundary on its way to declining"
    assert is_preserved(summary), "but the mark is back, because every pass re-applies it"
    assert summary.additional_properties[PRESERVE_REASON_KEY] == PRESERVE_REASON

    recompacting = _strategy()
    plain = _conversation(8)
    assert await recompacting(plain) is True
    assert not is_preserved(_standing(plain)[0]), (
        "the recompacting mode's summary must stay replaceable"
    )


async def test_the_head_is_not_re_counted_once_a_boundary_stands() -> None:
    """After a boundary the band starts at the boundary, and not at the head-th unprotected turn.

    On an ordinary conversation the two readings agree, because everything in front of the
    boundary is a head turn or a superseded one. They come apart when another strategy has
    claimed a head turn: re-counting the head over the unprotected turns would then land it on
    the first turn after the boundary and protect that turn for the rest of the run, which is
    neither the head nor the tail nor anything the caller asked for. So the rule is asserted on
    that case, where only the explicit boundary rule gets it right.
    """
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, summary_mode=SUMMARY_MODE_BOUNDARY)
    messages = _conversation(8)
    assert await strategy(messages) is True
    set_preserved(
        next(message for message in messages if message.message_id == "u0"),
        preserved=True,
        reason="a test",
    )
    messages += _conversation(6, first_turn=8)

    assert await strategy(messages) is True
    asked = summarizer.requests[1][-1].text or ""

    assert "Turn 7:" in asked, (
        "the first turn after the boundary is in the band, whatever happened to the head"
    )
    assert "Turn 0:" not in asked
    assert strategy.user_messages_replaced == 6


async def test_the_share_clears_less_often_after_a_boundary_and_the_numbers_are_recorded() -> None:
    """The part most likely to surprise, measured on the growing fixture rather than tuned away.

    In the recompacting mode the band the share is taken of includes the previous summary, which
    inflates it; after a boundary the band is only the turns newer than the boundary, and the
    prompt it is weighed against is larger by every standing summary. So the same share clears
    later. On the forty-turn lopsided fixture: the recompacting mode fires on turns 7, 13 and 23,
    the boundary mode on 7, 14 and 25 -- the same three passes, each later crossing one or two
    turns later, with thirty passes held in both. Not fewer passes on this fixture, because its
    summaries are twelve tokens; the user-heavy test below is where the count moves too.

    Asserted as an ordering rather than as those constants, because the constants are what a
    later change would quietly re-tune: the boundary mode fires no more often, never earlier,
    and strictly later at least once.
    """
    recompacting = _strategy()
    boundary = _strategy(summary_mode=SUMMARY_MODE_BOUNDARY)

    recompact_passes = await _grow_and_compact(recompacting, 40)
    boundary_passes = await _grow_and_compact(boundary, 40)
    recompact_fired = [index for index, _, fired in recompact_passes if fired]
    boundary_fired = [index for index, _, fired in boundary_passes if fired]

    assert len(recompact_fired) >= 2, "one pass cannot show the share being cleared again"
    assert len(boundary_fired) <= len(recompact_fired), (
        "no more passes with a boundary than without"
    )
    assert recompact_fired[0] == boundary_fired[0], (
        "the first pass is the same pass: there is no boundary yet"
    )
    assert all(
        later >= earlier for earlier, later in zip(recompact_fired, boundary_fired, strict=False)
    )
    assert any(
        later > earlier for earlier, later in zip(recompact_fired, boundary_fired, strict=False)
    ), (
        "and at least one later crossing clears the share strictly later, because the previous "
        "summary is no longer in the band -- a boundary mode that still read it would fire on the "
        "recompacting mode's turns exactly"
    )
    assert boundary.user_passes_declined >= recompacting.user_passes_declined
    assert boundary.user_summaries_in_conversation == boundary.user_compactions


async def test_the_three_modes_side_by_side_on_a_conversation_whose_summaries_matter() -> None:
    """The comparison the flag exists for, on the fixture where the arms come apart.

    Sixty user-heavy turns, a summarizer keeping 35% of what it reads, one share for all three.
    Measured: the recompacting mode fires 36 times and leaves one summary of 589 tokens in a
    12,030-token prompt; the boundary mode fires 20 times, holds 28, and leaves twenty summaries
    standing -- 20,855 tokens, 63% of a 33,325-token prompt, which is the accumulation the
    counter exists to show; the fold mode fires 26 times, folds 11 times, and leaves three
    summaries of 2,288 tokens in a 13,729-token prompt. So the fold brings the floor back to
    within a sixth of the recompacting prompt at a third of its whole-prefix breaks -- and none of
    that is a fidelity claim, because nothing here can measure what eleven generations of
    summary kept. Asserted as orderings, for the reason the test above gives.
    """
    runs: dict[str, UserTurnAnchoredSummarizationCompactionStrategy] = {}
    prompts: dict[str, int] = {}
    for mode in SUMMARY_MODES:
        strategy = _strategy(_RatioSummarizer(0.35), summary_mode=mode)
        messages, _ = await _grow_in_mode(strategy, 60)
        runs[mode] = strategy
        prompts[mode] = _included(messages)
        assert strategy.user_summaries_in_conversation == len(_standing(messages))
    recompacting, boundary, fold = (runs[mode] for mode in SUMMARY_MODES)

    assert recompacting.user_summaries_in_conversation == 1
    assert boundary.user_summaries_in_conversation == boundary.user_compactions > 1, (
        "N passes, N standing"
    )
    assert boundary.user_passes_declined > recompacting.user_passes_declined, (
        "the share bites harder"
    )
    assert boundary.user_summary_tokens > prompts[SUMMARY_MODE_BOUNDARY] // 2, (
        "the floor is most of the prompt"
    )
    assert prompts[SUMMARY_MODE_BOUNDARY] > prompts[SUMMARY_MODE_RECOMPACT], (
        "and it is the whole difference"
    )

    assert fold.user_folds > 0, (
        "the fixture has to reach a fold or the third arm is the second under a new name"
    )
    assert fold.user_summaries_in_conversation < boundary.user_summaries_in_conversation
    assert fold.user_summary_tokens < boundary.user_summary_tokens // 4, (
        "the fold lowered the floor"
    )
    assert prompts[SUMMARY_MODE_FOLD] < prompts[SUMMARY_MODE_BOUNDARY]
    assert fold.user_folds < recompacting.user_compactions, (
        "and paid fewer whole-prefix breaks than the recompacting mode pays passes, which is the trade"
    )


async def test_a_fold_needs_two_standing_summaries_and_so_never_follows_a_fold() -> None:
    """The hard precondition, checked pass by pass on a run that folds many times.

    Folding one summary is a rewrite of one message at one position -- the recompacting mode
    with extra steps -- so the fold path must be unreachable with fewer than two. That is also
    the whole of what stops one fold following another: a fold leaves one summary standing, so
    the next needs an ordinary pass first, and that pass is bounded by the share.
    """
    strategy = _strategy(_RatioSummarizer(0.35), summary_mode=SUMMARY_MODE_FOLD)

    _, events = await _grow_in_mode(strategy, 60)
    folds = [
        (turn, standing_before) for turn, standing_before, outcome in events if outcome == "folded"
    ]
    outcomes = [outcome for _, _, outcome in events]

    assert len(folds) >= 3, "the fixture has to fold repeatedly or the rule is not being tested"
    assert all(standing_before >= 2 for _, standing_before in folds), (
        "never with fewer than two standing"
    )
    assert "folded" not in [
        b for a, b in zip(outcomes, outcomes[1:], strict=False) if a == "folded"
    ], "and never on the pass after a fold, which left exactly one"
    assert strategy.user_summaries_in_conversation >= 1


def _fold_scenario(
    summarizer: Any, **kwargs: Any
) -> UserTurnAnchoredSummarizationCompactionStrategy:
    """Return a fold-mode strategy over the compacting ceiling."""
    return _strategy(summarizer, summary_mode=SUMMARY_MODE_FOLD, **kwargs)


async def test_a_single_standing_summary_is_never_folded_even_when_nothing_bounds_the_share() -> (
    None
):
    """The two-summary precondition is hard, and the share is not what enforces it.

    At any share above zero a fold of one summary is refused by arithmetic alone -- what it would
    remove is the summaries less the largest, which is nothing -- so the precondition is only
    load-bearing where the share is zero. That is the arm ``--user-min-band-share 0`` exists to
    run, and on it a fold of one summary would be a rewrite of one message at one position on
    every starved pass: the recompacting mode's whole defect, reached through the fold.
    """
    summarizer = _Summarizer()
    strategy = _fold_scenario(summarizer, min_band_share=0.0)
    messages = _conversation(8)
    assert await strategy(messages) is True
    assert len(_standing(messages)) == 1

    assert await strategy(messages) is False, "nothing newer than the boundary but the tail"

    assert (strategy.user_folds, strategy.user_passes_declined) == (0, 1), "declined, not folded"
    assert len(summarizer.requests) == 1, (
        "and the summarizer was not asked to rewrite one summary as one summary"
    )
    assert len(_standing(messages)) == 1


async def _three_standing(
    strategy: UserTurnAnchoredSummarizationCompactionStrategy,
) -> list[Message]:
    """Return the eight-turn fixture after three crossings, leaving three summaries standing."""
    messages = _conversation(8)
    assert await strategy(messages) is True
    for start in (8, 14):
        messages += _conversation(6, first_turn=start)
        assert await strategy(messages) is True
    assert len(_standing(messages)) == 3, "three crossings, three boundaries, no fold yet"
    return messages


def _fold_terms(messages: list[Message]) -> tuple[int, int, int]:
    """Return the fold's ``R``, the undeducted total, and ``B``, computed from the conversation.

    Written out here rather than read off the strategy, so a test can state the fixture's ratio
    as its own premise: what the fold would remove is the standing summaries less the largest,
    and what it would re-bill is the included prompt from the oldest of them to the end. The
    undeducted total is returned beside ``R`` so a test can sit a share between the two and
    catch a fold that forgot the deduction.
    """
    _included(messages)
    standing = _standing(messages)
    sizes = [included_token_count([message]) for message in standing]
    behind = included_token_count(messages[messages.index(standing[0]) :])
    return sum(sizes) - max(sizes), sum(sizes), behind


async def test_a_fold_is_declined_until_the_standing_summaries_repay_the_break() -> None:
    """The second condition, at its own boundary: the break-even, not the band having stalled.

    Three standing summaries of about 790 tokens each on the eight-turn fixture are 1,584
    removable tokens against 22,985 behind the oldest of them -- about 7% -- and then one
    tiny turn arrives, so the band is starved. At the default share of a tenth that is not a
    fold, and the pass is declined with the summarizer untouched; at a share of a twentieth the
    same conversation folds. The fixture's ratio is asserted between the two shares first, so
    that the test cannot go vacuous by the summaries drifting to either side of both. A third
    share sits between the deducted ratio and the undeducted one -- 7% against 10% -- and is
    declined too, which is what pins the deduction: a fold that counted the whole of the
    standing summaries as its removal would fold there, and at the default share as well.
    """
    large = "x" * _LARGE_SUMMARY_CHARS
    declining_summarizer = _Summarizer(large)
    declining = _fold_scenario(declining_summarizer)
    declining_messages = await _three_standing(declining)
    folding = _fold_scenario(_Summarizer(large), min_band_share=0.05)
    folding_messages = await _three_standing(folding)
    undeducted = _fold_scenario(_Summarizer(large), min_band_share=0.08)
    undeducted_messages = await _three_standing(undeducted)
    removable, total, behind = _fold_terms(declining_messages)
    assert (
        _fold_terms(folding_messages)
        == _fold_terms(undeducted_messages)
        == (removable, total, behind)
    ), "one conversation, three shares"
    assert 0.05 * behind <= removable < 0.08 * behind <= total, (
        f"the premise: {removable} removable and {total} undeducted against {behind} behind"
    )
    assert DEFAULT_MIN_BAND_SHARE > 0.08, (
        "so the default share declines it too, whichever removal is counted"
    )

    declining_messages += _conversation(1, first_turn=20)
    folding_messages += _conversation(1, first_turn=20)
    undeducted_messages += _conversation(1, first_turn=20)
    requests_before = len(declining_summarizer.requests)

    assert await declining(declining_messages) is False
    assert (declining.user_folds, declining.user_passes_declined) == (0, 1), (
        "not worth the break: declined"
    )
    assert len(declining_summarizer.requests) == requests_before, (
        "and no call spent finding that out"
    )
    assert len(_standing(declining_messages)) == 3

    assert await folding(folding_messages) is True
    assert (folding.user_folds, folding.user_passes_declined) == (1, 0), (
        "worth it at the lower share: folded"
    )
    assert len(_standing(folding_messages)) == 1

    assert await undeducted(undeducted_messages) is False
    assert (undeducted.user_folds, undeducted.user_passes_declined) == (0, 1), (
        "declined at the share between the two ratios: the largest summary is not counted as removed"
    )


async def test_a_fold_collapses_every_standing_summary_into_one_new_boundary() -> None:
    """What a fold does, step by step, and that the cycle continues behind it.

    The fold's output has to be a boundary by the same test as any other summary, stand where
    the oldest summary stood, carry the ids of everything it folded, and be preserved; each
    folded summary has to be excluded under the fold's own reason, point back at the fold, and
    be released from preservation, so that "preserved" and "included" go on meaning one thing.
    And the summarizer has to have been told it was reading summaries, with the marker stripped.
    Then one more crossing: the band starts after the fold, and the fold is not re-read.
    """
    summarizer = _Summarizer("x" * _LARGE_SUMMARY_CHARS)
    strategy = _fold_scenario(summarizer, min_band_share=0.05)
    messages = await _three_standing(strategy)
    folded = _standing(messages)
    oldest_index = messages.index(folded[0])
    messages += _conversation(1, first_turn=20)
    replies_before = [
        m.message_id for m in project_included_messages(messages) if m.role == "assistant"
    ]

    assert await strategy(messages) is True
    (fold,) = _standing(messages)

    assert (fold.message_id or "").startswith(FOLD_ID_PREFIX) and _is_summary(fold)
    assert messages.index(fold) == oldest_index, "it stands where the oldest summary stood"
    assert _summary_of_message_ids(fold) == [m.message_id for m in folded]
    assert len(_summary_of_group_ids(fold)) == 3
    assert is_preserved(fold) and fold.additional_properties[PRESERVE_REASON_KEY] == PRESERVE_REASON
    for message in folded:
        assert message.additional_properties[EXCLUDED_KEY] is True
        assert message.additional_properties[EXCLUDE_REASON_KEY] == FOLD_EXCLUDE_REASON
        assert _summarized_by(message) == fold.message_id
        assert not is_preserved(message), "released: the promise travels to the replacement"
    system, transcript = summarizer.requests[-1]
    assert system.text == DEFAULT_USER_FOLD_PROMPT, (
        "the summarizer was told it was reading summaries"
    )
    assert USER_SUMMARY_MARKER not in (transcript.text or ""), (
        "and was not sent the marker as content"
    )
    assert (transcript.text or "").count("\n") == 2, "three numbered lines, one per folded summary"
    assert [
        m.message_id for m in project_included_messages(messages) if m.role == "assistant"
    ] == replies_before, "nothing between the summaries was touched"
    assert _user_texts(messages)[0].startswith("Turn 0:"), "nor the head"
    assert strategy.user_summaries_in_conversation == 1
    assert strategy.user_summary_tokens == included_token_count([fold])

    messages += _conversation(6, first_turn=21)
    assert await strategy(messages) is True
    asked = summarizer.requests[-1][-1].text or ""
    assert "Turn 20:" in asked and "Turn 21:" in asked, (
        "the band after the fold starts at the first turn newer than it"
    )
    assert USER_SUMMARY_MARKER not in asked and "x" * 100 not in asked, (
        "and the fold itself is not in it"
    )
    assert len(_standing(messages)) == 2 and _standing(messages)[0] is fold


async def test_a_fold_whose_summarizer_fails_leaves_the_standing_summaries_as_they_were() -> None:
    """Degrading safely is the fold's contract too: no text, no supersession, and it is counted."""
    strategy = _fold_scenario(_Summarizer("x" * _LARGE_SUMMARY_CHARS), min_band_share=0.05)
    messages = await _three_standing(strategy)
    messages += _conversation(1, first_turn=20)
    standing = _standing(messages)
    strategy.client = cast(Any, _FailingSummarizer())

    assert await strategy(messages) is False

    assert _standing(messages) == standing
    assert all(is_preserved(message) for message in standing), "still boundaries, still protected"
    assert (strategy.user_folds, strategy.user_summary_failures, strategy.user_passes_declined) == (
        0,
        1,
        0,
    )


@pytest.mark.parametrize("mode", SUMMARY_MODES)
async def test_a_fresh_instance_numbers_its_summary_past_the_ones_the_conversation_carries(
    mode: str,
) -> None:
    """An id is unique against the conversation, not against the instance that minted it.

    A strategy is built per run and a conversation outlives one: restored from a store, or
    handed to a new instance over the same list. Numbered from the instance's own count alone,
    the new instance's first summary was ``user_summary_0`` again -- beside the
    ``user_summary_0`` already there, standing in the boundary modes and superseded in the
    recompacting one -- and every back-reference written under that id then named two messages.
    So the number is read off the conversation, superseded summaries included, and the
    assertion is on the conversation: no id names two messages, and every back-reference
    resolves to exactly one.
    """
    first_instance = _strategy(summary_mode=mode)
    messages = _conversation(8)
    assert await first_instance(messages) is True
    messages += _conversation(6, first_turn=8)

    second_instance = _strategy(summary_mode=mode)
    assert await second_instance(messages) is True

    summaries = [message for message in messages if _is_summary(message)]
    assert sorted(message.message_id or "" for message in summaries) == [
        f"{SUMMARY_ID_PREFIX}0",
        f"{SUMMARY_ID_PREFIX}1",
    ]
    all_ids = [message.message_id for message in messages]
    assert len(all_ids) == len(set(all_ids)), "no id names two messages"
    referenced = {_summarized_by(message) for message in messages} - {None}
    assert referenced == {message.message_id for message in summaries}
    for summary_id in referenced:
        assert sum(1 for message in messages if message.message_id == summary_id) == 1
    assert second_instance.user_compactions == 1, (
        "the count stays the instance's; only the number is the conversation's"
    )


async def test_a_fresh_instance_numbers_its_fold_past_the_fold_the_conversation_carries() -> None:
    """The fold's id follows the same rule under its own prefix, and the two prefixes stay apart.

    The fold prefix sits under the summary prefix so that a fold's output is a boundary by the
    ordinary test; the numbering has to keep the two apart, or ``user_summary_fold_0`` would be
    read as a summary numbered nothing and an ordinary ``user_summary_1`` as a fold.
    """
    summarizer = _Summarizer("x" * _LARGE_SUMMARY_CHARS)
    first_instance = _fold_scenario(summarizer, min_band_share=0.05)
    messages = await _three_standing(first_instance)
    messages += _conversation(1, first_turn=20)
    assert await first_instance(messages) is True
    assert first_instance.user_folds == 1
    messages += _conversation(6, first_turn=21)
    assert await first_instance(messages) is True
    assert [m.message_id for m in _standing(messages)] == [
        f"{FOLD_ID_PREFIX}0",
        f"{SUMMARY_ID_PREFIX}3",
    ]

    # A share of zero, so the fresh instance folds on the first pass whose band is empty --
    # which is this one: nothing newer than the newest boundary but the tail.
    second_instance = _fold_scenario(summarizer, min_band_share=0.0)
    assert await second_instance(messages) is True
    (fold,) = _standing(messages)

    assert fold.message_id == f"{FOLD_ID_PREFIX}1"
    assert (second_instance.user_folds, second_instance.user_compactions) == (1, 0)
    all_ids = [message.message_id for message in messages]
    assert len(all_ids) == len(set(all_ids)), "no id names two messages"


async def test_a_slowly_growing_conversation_does_not_fold_repeatedly() -> None:
    """The thrash guard, on the shape that would thrash: a starved band and a prompt that creeps.

    Thirty user-heavy turns with a summarizer keeping half of what it reads fold six times, and
    that is the fold working on a conversation whose summaries are a large share of it. Then
    sixty turns too small to move anything: the band stays starved, the prompt creeps up, and a
    fold on every starved pass would break the whole prefix sixty times to free a few hundred
    tokens each. Measured, one fold in those sixty turns, and what stops it is the two
    conditions together -- a fold leaves one summary, the next needs an ordinary pass first, an
    ordinary pass needs a band worth a tenth of the prompt, and fifty tiny turns are what that
    takes.
    """
    strategy = _fold_scenario(_RatioSummarizer(0.5))
    messages, events = await _grow_in_mode(strategy, 30)
    heavy_folds = strategy.user_folds
    assert heavy_folds >= 3, "the fixture has to be one that folds while it is growing fast"

    _, slow_events = await _grow_in_mode(
        strategy, 60, turn=_tiny_turn, messages=messages, first_turn=30
    )
    slow_folds = strategy.user_folds - heavy_folds

    assert slow_folds <= 1, (
        f"{slow_folds} folds while creeping, against {heavy_folds} while growing"
    )
    assert all(
        standing_before >= 2
        for _, standing_before, outcome in events + slow_events
        if outcome == "folded"
    )
    assert strategy.user_passes_declined + strategy.user_passes_below_trigger >= 50, (
        "the creep was mostly refused"
    )


async def test_the_five_outcomes_of_a_pass_partition_the_passes_in_the_fold_mode() -> None:
    """The partition the four-outcome test pins, with the fifth outcome present and non-zero."""
    strategy = _fold_scenario(_RatioSummarizer(0.35))
    _, events = await _grow_in_mode(strategy, 60)

    assert strategy.user_folds > 0
    assert (
        strategy.user_compactions
        + strategy.user_folds
        + strategy.user_passes_declined
        + strategy.user_passes_below_trigger
        + strategy.user_summary_failures
        + strategy.user_summaries_replayed
        == len(events)
    ), "every pass lands in exactly one of the six, and a list-driven run never replays"


# region replay: the same band presented twice


async def test_the_same_band_presented_twice_is_summarised_once_and_replayed_the_second_time() -> (
    None
):
    """Live, one conversation reaches this strategy as two lists, and the second must cost nothing.

    The harness runs a strategy inside the model call on the copies the history provider loaded
    and again after the turn on what it stored, and the copies' flags never reach the store: the
    after-turn pass finds the band the in-call pass already replaced. A second summarizer call
    there would spend a call and send a different summary at one position on consecutive calls.
    Two views of
    one list stand in for the two lists here, with a summarizer whose answers differ so that a
    second call would show as different bytes rather than pass by coincidence.
    """
    summarizer = _NumberedSummarizer()
    strategy = _strategy(summarizer, summary_mode=SUMMARY_MODE_BOUNDARY)
    loaded = _conversation(8)
    stored = deepcopy(loaded)

    assert await strategy(loaded) is True
    assert await strategy(stored) is True

    assert len(summarizer.requests) == 1, "one band, one request"
    (sent,) = _standing(loaded)
    (kept,) = _standing(stored)
    assert (kept.message_id, kept.text) == (sent.message_id, sent.text), (
        "the store carries what the model was sent"
    )
    assert _rendered(stored) == _rendered(loaded)
    assert is_preserved(kept), "and the replay is a boundary like any other"
    assert (strategy.user_compactions, strategy.user_summaries_replayed) == (1, 1)
    assert strategy.user_messages_replaced == 6
    assert strategy.user_summaries_in_conversation == 1


async def test_a_band_that_has_grown_is_a_new_request_and_not_a_replay() -> None:
    """Byte-identical is the test: one more turn in the band is another request, with the next id.

    The ids number the compactions and not the passes, which is what keeps the boundary mode's
    standing summaries numbered consecutively in the store rather than skipping the ids the
    in-call passes took.
    """
    summarizer = _NumberedSummarizer()
    strategy = _strategy(summarizer, summary_mode=SUMMARY_MODE_BOUNDARY)
    loaded = _conversation(8)
    stored = deepcopy(loaded)
    assert await strategy(loaded) is True
    assert await strategy(stored) is True

    stored += _conversation(6, first_turn=8)
    assert await strategy(stored) is True

    assert len(summarizer.requests) == 2, "the second crossing is a second request"
    assert (strategy.user_compactions, strategy.user_summaries_replayed) == (2, 1)
    assert [m.message_id for m in _standing(stored)] == [
        f"{SUMMARY_ID_PREFIX}0",
        f"{SUMMARY_ID_PREFIX}1",
    ]


async def test_a_failed_request_is_not_remembered_so_the_next_view_asks_again() -> None:
    """A summarizer that did not answer left nothing to replay, and the second view must ask."""
    strategy = _strategy(_FailingSummarizer(), summary_mode=SUMMARY_MODE_BOUNDARY)
    loaded = _conversation(8)
    stored = deepcopy(loaded)
    assert await strategy(loaded) is False
    strategy.client = cast(Any, _NumberedSummarizer())

    assert await strategy(stored) is True

    assert (
        strategy.user_summary_failures,
        strategy.user_compactions,
        strategy.user_summaries_replayed,
    ) == (1, 1, 0)
    assert len(_standing(stored)) == 1 and not _standing(loaded)


async def test_a_fold_presented_twice_is_folded_once_and_replayed_the_second_time() -> None:
    """The fold is the other summarizer request, and the live path presents it twice as well."""
    summarizer = _Summarizer("x" * _LARGE_SUMMARY_CHARS)
    strategy = _fold_scenario(summarizer, min_band_share=0.05)
    loaded = await _three_standing(strategy)
    loaded += _conversation(1, first_turn=20)
    stored = deepcopy(loaded)
    requests_before = len(summarizer.requests)

    assert await strategy(loaded) is True
    assert await strategy(stored) is True

    assert strategy.user_folds == 1, "one fold"
    assert len(summarizer.requests) == requests_before + 1, "one request for it"
    (sent,) = _standing(loaded)
    (kept,) = _standing(stored)
    assert (kept.message_id, kept.text) == (sent.message_id, sent.text)
    assert (kept.message_id or "").startswith(FOLD_ID_PREFIX)
    assert strategy.user_summaries_replayed == 1


# endregion


def _is_summary(message: Message) -> bool:
    """Return whether ``message`` is one of the strategy's summaries.

    Written out here rather than imported, so a test cannot agree with the implementation by
    sharing its mistake: if the strategy stopped recognising its own output, the private helper
    would say so too and the recompaction test above would pass on a conversation carrying two
    summaries.
    """
    return (message.message_id or "").startswith(SUMMARY_ID_PREFIX)


def _summarized_by(message: Message) -> str | None:
    """Return the id of the summary that superseded ``message``, if one did."""
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    return annotation.get(SUMMARIZED_BY_SUMMARY_ID_KEY) if isinstance(annotation, dict) else None


def _summary_of_message_ids(message: Message) -> list[str]:
    """Return the message ids a summary says it stands for."""
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    return (
        list(annotation.get(SUMMARY_OF_MESSAGE_IDS_KEY, [])) if isinstance(annotation, dict) else []
    )


def _summary_of_group_ids(message: Message) -> list[str]:
    """Return the group ids a summary says it stands for."""
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    return (
        list(annotation.get(SUMMARY_OF_GROUP_IDS_KEY, [])) if isinstance(annotation, dict) else []
    )
