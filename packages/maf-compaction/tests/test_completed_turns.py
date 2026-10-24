"""Completed requests remain visibly answered after shedding and session replay."""

from __future__ import annotations

import copy

import pytest
from agent_framework import CharacterEstimatorTokenizer, Content, Message
from agent_framework._compaction import (
    annotate_message_groups,
    group_messages,
    included_token_count,
    project_included_messages,
)

from maf_compaction._anchored import AnchoredCompactionStrategy, _is_marker
from maf_compaction._composed import (
    CompactionBudgetExceeded,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
)
from maf_compaction._toolsummary import (
    ToolResultAnchoredSummarizationCompactionStrategy,
    build_record_message,
)
from maf_compaction._usersummary import UserTurnAnchoredSummarizationCompactionStrategy

pytestmark = pytest.mark.anyio
TOKENIZER = CharacterEstimatorTokenizer()
ANSWERED = "the preceding user request was already answered"


class _UnusedSummarizer:
    async def get_response(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("The fixture retains every user request.")


def _strategy(budget: int = 6_000) -> ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy:
    return ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=TOKENIZER,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=budget,
            tokenizer=TOKENIZER,
            keep_head_groups=0,
            keep_tail_groups=1,
        ),
        user_turns=UserTurnAnchoredSummarizationCompactionStrategy(
            max_input_tokens=budget,
            tokenizer=TOKENIZER,
            client=_UnusedSummarizer(),
            keep_head_user_turns=100,
        ),
        harder_attempts=0,
    )


def _conversation(reply_padding: int) -> list[Message]:
    messages: list[Message] = []
    for index in range(32):
        call_id = f"fetch_{index}"
        messages.extend(
            [
                Message(
                    role="user",
                    contents=[f"Fetch page {index} and reply with its title only."],
                    message_id=f"user_{index}",
                ),
                Message(
                    role="assistant",
                    contents=[Content.from_function_call(call_id, "fetch", arguments="{}")],
                    message_id=f"call_{index}",
                ),
                Message(
                    role="tool",
                    contents=[Content.from_function_result(call_id, result="document " * 1_000)],
                    message_id=f"result_{index}",
                ),
                Message(
                    role="assistant",
                    contents=[f"Title {index}" + "x" * reply_padding],
                    message_id=f"reply_{index}",
                ),
            ]
        )
    messages.append(Message(role="user", contents=["Fetch the next page."], message_id="latest"))
    return messages


def _assert_answered(messages: list[Message]) -> None:
    projected = project_included_messages(messages)
    for index in range(32):
        start = next(i for i, m in enumerate(projected) if m.message_id == f"user_{index}")
        end = next(i for i in range(start + 1, len(projected)) if projected[i].role == "user")
        assert any(
            f"Title {index}" in m.text or ANSWERED in m.text for m in projected[start + 1 : end]
        ), f"request {index} lost its answer status"
    assert projected[-1].message_id == "latest"


@pytest.mark.parametrize("reply_padding", [0, 1_000])
@pytest.mark.parametrize("has_record", [False, True])
async def test_many_completed_turns_stay_answered_after_overflow_and_restore(
    reply_padding: int,
    has_record: bool,
) -> None:
    messages = _conversation(reply_padding)
    if has_record:
        messages.insert(0, build_record_message("No page content retained."))
    original = copy.deepcopy(messages)
    strategy = _strategy()

    assert await strategy(messages)
    assert included_token_count(messages) <= strategy.max_input_tokens
    assert any(_is_marker(m) for m in messages), "the fixture must shed whole groups"
    _assert_answered(messages)
    if reply_padding == 0:
        assert all(
            any(m.message_id == f"reply_{i}" for m in project_included_messages(messages))
            for i in range(32)
        )
    else:
        assert any(ANSWERED in m.text for m in project_included_messages(messages))

    restored = _strategy(500_000 if has_record else 6_000)
    restored.restore_session_state(strategy.session_state())
    await restored(original)
    _assert_answered(original)
    expected = [(m.role, m.message_id, m.text) for m in project_included_messages(messages)]
    assert [(m.role, m.message_id, m.text) for m in project_included_messages(original)] == expected
    await restored(original)
    assert [(m.role, m.message_id, m.text) for m in project_included_messages(original)] == expected


async def test_short_answers_survive_when_the_retained_requests_exhaust_the_budget() -> None:
    messages = _conversation(0)
    with pytest.raises(CompactionBudgetExceeded):
        await _strategy(1)(messages)
    _assert_answered(messages)
    assert all(
        any(m.message_id == f"reply_{i}" for m in project_included_messages(messages))
        for i in range(32)
    )


@pytest.mark.parametrize("reply_tokens", [64, 65])
async def test_short_reply_threshold_uses_the_configured_tokenizer(reply_tokens: int) -> None:
    class _Words:
        def count_tokens(self, text: str) -> int:
            return len(text.split())

    messages = [
        Message(role="user", contents=["Question"], message_id="u"),
        Message(role="assistant", contents=["answer " * reply_tokens], message_id="a"),
        Message(role="user", contents=["Next question"], message_id="next"),
    ]
    strategy = AnchoredCompactionStrategy(
        max_input_tokens=1, tokenizer=_Words(), keep_head_groups=0, keep_tail_groups=0
    )
    await strategy(messages)
    projected = project_included_messages(messages)
    assert any(m.message_id == "a" for m in projected) == (reply_tokens == 64)
    assert any(ANSWERED in m.text for m in projected) == (reply_tokens == 65)


async def test_intermediate_narration_and_a_tool_result_do_not_mark_a_request_answered() -> None:
    messages = _conversation(1_000)[:3]
    messages.insert(
        1, Message(role="assistant", contents=["I will fetch it."], message_id="progress")
    )
    messages.append(Message(role="user", contents=["Cancel that."], message_id="cancel"))
    strategy = AnchoredCompactionStrategy(
        max_input_tokens=1, tokenizer=TOKENIZER, keep_head_groups=0, keep_tail_groups=0
    )
    await strategy(messages)
    assert any(_is_marker(m) for m in messages)
    assert not any(ANSWERED in m.text for m in project_included_messages(messages))


def test_replaying_a_legacy_shed_short_reply_leaves_an_answered_marker() -> None:
    messages = _conversation(0)
    strategy = AnchoredCompactionStrategy(max_input_tokens=4_000, tokenizer=TOKENIZER)
    assert strategy.shed_again(messages, ["reply_0"])
    _assert_answered(messages)
    note = next(m for m in messages if ANSWERED in m.text)
    assert _is_marker(note)
    assert not strategy.shed_again(messages, ["reply_0"])
    annotate_message_groups(messages)
    assert all(
        not strategy._is_marker_group(messages, g)
        for g in strategy._middle_band(messages, group_messages(messages))
    )
