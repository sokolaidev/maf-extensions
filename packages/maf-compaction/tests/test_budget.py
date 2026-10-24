"""Budget enforcement with uncovered tables, replay, and protected history."""

from __future__ import annotations

import json
from copy import deepcopy
from math import ceil

import pytest
from agent_framework import CharacterEstimatorTokenizer, ChatContext, ChatResponse, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    annotate_token_counts,
    included_token_count,
    project_included_messages,
)

from maf_compaction import (
    DEFAULT_CHAIN_GAIN_FRACTION,
    AnchoredCompactionStrategy,
    CompactionBudgetExceeded,
    CompactionStack,
    RecallGate,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    UserTurnAnchoredSummarizationCompactionStrategy,
    set_preserved,
)
from maf_compaction._toolsummary import build_record_message

pytestmark = pytest.mark.anyio
TOKENIZER = CharacterEstimatorTokenizer()
BUDGET = 36_000


class RefusingSummarizer:
    async def get_response(self, messages, **kwargs):
        return ChatResponse(messages=Message("assistant", [messages[-1].text * 2]))


def stack(*, policy="shed", budget=BUDGET, fraction=DEFAULT_CHAIN_GAIN_FRACTION):
    records = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=budget,
        tokenizer=TOKENIZER,
        keep_head_groups=1,
        trigger_fraction=0.05,
        fallback=AnchoredCompactionStrategy(
            max_input_tokens=budget,
            tokenizer=TOKENIZER,
            keep_head_groups=1,
            keep_tail_groups=1,
        ),
    )
    users = UserTurnAnchoredSummarizationCompactionStrategy(
        max_input_tokens=budget,
        tokenizer=TOKENIZER,
        client=RefusingSummarizer(),
        summary_mode="boundary",
        remembered_requests=2,
    )
    composed = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=TOKENIZER,
        tool_results=records,
        user_turns=users,
        overflow_policy=policy,
        chain_gain_fraction=fraction,
    )
    gate = RecallGate()
    recall = ToolResultRecallMiddleware(
        max_input_tokens=budget,
        tokenizer=TOKENIZER,
        arm=gate.arm,
        reforce=records.take_reforce,
    )
    return CompactionStack(composed, gate, recall)


def table(index):
    body = "\n".join(f"| SKU-{index}-{row:04d} | {10000 + row} |" for row in range(1000))
    return [
        Message(
            "assistant",
            [
                {
                    "type": "function_call",
                    "name": "lookup",
                    "arguments": "{}",
                    "call_id": f"call{index}",
                }
            ],
            message_id=f"c{index}",
        ),
        Message(
            "tool",
            [{"type": "function_result", "call_id": f"call{index}", "result": body}],
            message_id=f"r{index}",
        ),
    ]


def conversation():
    messages = [Message("user", ["Compare the tables."], message_id="task")]
    for index in range(10):
        messages += table(index)
    messages += [build_record_message("Only SKU-0-0000 was recorded.")]
    messages += [Message("assistant", ["Continue."], message_id="tail")]
    return messages


def size(messages):
    annotate_token_counts(messages, tokenizer=TOKENIZER, force_retokenize=True)
    return included_token_count(messages)


async def test_uncovered_tables_fit_and_shedding_survives_cold_restore(caplog):
    first = stack()
    messages = conversation()
    stored = deepcopy(messages)
    assert size(messages) > BUDGET
    assert await first.strategy(messages)
    assert size(messages) <= BUDGET
    lost = {m.message_id for m in messages if m.additional_properties.get(EXCLUDED_KEY)}
    assert lost
    assert "lost message IDs" in caplog.text
    assert "SKU-" not in caplog.text
    for index in range(10):
        assert (f"c{index}" in lost) == (f"r{index}" in lost)
    cold = stack()
    cold.restore(json.loads(json.dumps(first.to_dict())))
    await cold.strategy(stored)
    assert size(stored) <= BUDGET
    assert [m.to_dict() for m in project_included_messages(stored)] == [
        m.to_dict() for m in project_included_messages(messages)
    ]
    for index in range(10, 22):
        stored += table(index)
        stored += [build_record_message(f"Only SKU-{index}-0000 was recorded.")]
        stored += [Message("assistant", ["Continue."], message_id=f"tail{index}")]
        await cold.strategy(stored)
        assert size(stored) <= BUDGET


async def test_strict_overflow_keeps_uncovered_results():
    messages = conversation()
    with pytest.raises(CompactionBudgetExceeded) as error:
        await stack(policy="raise").strategy(messages)
    assert error.value.tokens == size(messages) > error.value.budget == BUDGET
    assert all(not m.additional_properties.get(EXCLUDED_KEY) for m in messages if m.role == "tool")


@pytest.mark.parametrize("fraction", [0.0, DEFAULT_CHAIN_GAIN_FRACTION, 0.6])
@pytest.mark.parametrize("protected_groups", [0, 2])
async def test_overflow_shed_reserves_headroom_behind_its_first_removal(
    fraction, protected_groups, caplog
):
    messages = conversation()
    messages[-2].message_id = "record"
    for message in messages[1 : 1 + 2 * protected_groups]:
        set_preserved(message, preserved=True, reason="host")
    original = deepcopy(messages)
    budget = size(messages) - 100
    current = stack(budget=budget, fraction=fraction)
    await current.strategy(messages)
    lost = {m.message_id for m in messages if m.additional_properties.get(EXCLUDED_KEY)}
    first = next(i for i, m in enumerate(original) if m.message_id in lost)
    assert first == 1 + 2 * protected_groups
    target = budget - ceil(fraction * size(original[first:]))
    assert size(messages) <= target
    assert caplog.text.count("lost message IDs") == 1
    if fraction == 0:
        assert lost == {f"c{protected_groups}", f"r{protected_groups}"}
    else:
        assert len(lost) > 2
    cold = stack(budget=budget, fraction=fraction)
    cold.restore(json.loads(json.dumps(current.to_dict())))
    caplog.clear()
    await cold.strategy(original)
    assert [m.to_dict() for m in project_included_messages(original)] == [
        m.to_dict() for m in project_included_messages(messages)
    ]
    assert "lost message IDs" not in caplog.text


async def test_overflow_headroom_allows_more_tool_results_without_another_shed(caplog):
    messages = conversation()
    budget = size(messages) - 100
    current = stack(budget=budget)
    await current.strategy(messages)
    lost = {m.message_id for m in messages if m.additional_properties.get(EXCLUDED_KEY)}
    caplog.clear()
    for index in range(10, 12):
        messages += table(index)
        await current.strategy(messages)
        assert size(messages) <= budget
        assert {m.message_id for m in messages if m.additional_properties.get(EXCLUDED_KEY)} == lost
    assert "lost message IDs" not in caplog.text


async def test_overflow_headroom_uses_the_sending_budget():
    messages = conversation()
    budget = size(messages) + 1000
    current = stack(budget=budget)
    options = {"instructions": "instructions " * 1000}
    sending_budget = current.strategy.sending_budget(options)
    assert sending_budget < size(messages) < budget
    behind = size(messages[1:])
    await current.strategy.compact_before(messages, options=options)
    assert size(messages) <= sending_budget - ceil(DEFAULT_CHAIN_GAIN_FRACTION * behind)


async def test_unreachable_headroom_keeps_protected_material_when_it_fits():
    messages = conversation()
    protected = build_record_message("fact123 " * 15_000)
    original_text = protected.text
    messages.insert(1, protected)
    current = stack(fraction=0.95)
    await current.strategy(messages)
    assert size(messages) <= BUDGET
    assert not protected.additional_properties.get(EXCLUDED_KEY)
    assert protected.text == original_text
    assert all(m.additional_properties.get(EXCLUDED_KEY) for m in messages if m.role == "tool")


@pytest.mark.parametrize("group_bound", [None, 2])
async def test_shed_groups_do_not_rearm_recall(group_bound):
    current = stack()
    messages = [
        Message("user", ["Compare the tables."], message_id="task"),
        build_record_message("fact123 " * 15_000),
        *(message for index in range(10) for message in table(index)),
        Message("assistant", ["Continue."], message_id="tail"),
    ]
    await current.strategy(messages)
    assert BUDGET * 0.8 < size(messages) <= BUDGET
    assert all(m.additional_properties.get(EXCLUDED_KEY) for m in messages if m.role == "tool")
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=BUDGET,
        tokenizer=TOKENIZER,
        arm=current.gate.arm,
        reforce=current.middleware.reforce,
        repeat_records=group_bound is None,
        max_groups_before_record=group_bound,
    )

    async def call():
        context = ChatContext(
            None, messages, {"tools": [{"type": "function", "name": "recall_earlier_tool_results"}]}
        )

        seen = {}

        async def respond():
            seen.update(context.options or {})
            context.result = ChatResponse(messages=Message("assistant", ["Continue."]))

        await middleware.process(context, respond)
        return seen

    for _ in range(5):
        assert "tool_choice" not in await call()
    for index in range(group_bound or 4):
        added = table(10 + index)
        added[-1].contents[0].result = "new123"
        messages += added
    await call()
    assert "tool_choice" in await call()


async def test_cold_replay_removes_shed_groups_even_below_the_trigger(caplog):
    first = stack()
    messages = conversation()
    await first.strategy(messages)
    lost = {m.message_id for m in messages if m.additional_properties.get(EXCLUDED_KEY)}
    index = next(index for index in range(10) if f"c{index}" in lost)
    cold = stack()
    cold.restore(json.loads(json.dumps(first.to_dict())))
    stored = [Message("user", ["task"], message_id="task"), *table(index)]
    stored[-1].contents[0].result = "small123"
    assert size(stored) < BUDGET * 0.05
    caplog.clear()
    await cold.strategy(stored)
    assert all(m.additional_properties.get(EXCLUDED_KEY) for m in stored if m.message_id in lost)
    assert "lost message IDs" not in caplog.text


@pytest.mark.parametrize("protection", ["host", "mixed_attribution"])
async def test_shedding_keeps_external_holds_and_mixed_call_result_groups(protection):
    messages = conversation()
    call = next(m for m in messages if m.message_id == "c0")
    if protection == "host":
        set_preserved(call, preserved=True, reason="host")
    else:
        call.additional_properties["_attribution"] = {"source_id": "history"}
    await stack().strategy(messages)
    assert size(messages) <= BUDGET
    assert all(
        not m.additional_properties.get(EXCLUDED_KEY)
        for m in messages
        if m.message_id in {"c0", "r0"}
    )


@pytest.mark.parametrize("kind", ["record", "external", "anchor"])
async def test_irreducible_protected_material_raises(kind):
    if kind == "record":
        protected = build_record_message("fact123 " * 30_000)
    else:
        protected = Message("user", ["x" * 200_000], message_id="protected")
        if kind == "external":
            set_preserved(protected, preserved=True, reason="host")
    messages = [protected]
    with pytest.raises(CompactionBudgetExceeded):
        await stack().strategy(messages)
    assert not protected.additional_properties.get(EXCLUDED_KEY)


def test_invalid_overflow_policy_is_rejected():
    with pytest.raises(ValueError, match="overflow_policy"):
        stack(policy="silently drop")
