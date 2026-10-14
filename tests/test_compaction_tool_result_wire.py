"""Compacted tool results must agree with the provider request payload."""

from __future__ import annotations

import pytest
from agent_framework import CharacterEstimatorTokenizer, Content, Message
from agent_framework._compaction import included_token_count, project_included_messages
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from maf_compaction import AnchoredCompactionStrategy, MinimumGainAnchoredCompactionStrategy
from maf_compaction._toolsummary import (
    ToolResultAnchoredSummarizationCompactionStrategy,
    _table_rows,
)

pytestmark = pytest.mark.anyio
TOKENIZER = CharacterEstimatorTokenizer()


@pytest.mark.parametrize("shape", ["legacy", "empty_items", "text", "multiple_text"])
@pytest.mark.parametrize(
    "strategy_type", [AnchoredCompactionStrategy, MinimumGainAnchoredCompactionStrategy]
)
async def test_shortening_reaches_both_openai_request_formats(shape, strategy_type):
    text = "head " + "original-middle " * 2_000 + " tail"
    if shape in {"legacy", "empty_items"}:
        content = Content("function_result", call_id="lookup-1", result=text)
        if shape == "empty_items":
            content.items = []
    else:
        result = (
            text if shape == "text" else [Content.from_text(text), Content.from_text("second part")]
        )
        content = Content.from_function_result(call_id="lookup-1", result=result)
    content.additional_properties["host_note"] = "retain"
    messages = [
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup-1", name="lookup", arguments="{}")],
        ),
        Message("tool", [content]),
    ]
    strategy = strategy_type(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        keep_head_groups=0,
        keep_tail_groups=0,
        keep_tokens=100,
    )

    assert await strategy(messages)
    assert project_included_messages(messages) == messages
    assert included_token_count(messages) <= 1_000
    assert content.call_id == "lookup-1"
    assert content.additional_properties["host_note"] == "retain"
    assert "original-middle " * 100 not in content.result
    assert content.result.endswith(" tail" if shape != "multiple_text" else "second part")
    _assert_wire_text(messages, content)
    saved = content.to_dict()
    assert not await strategy(messages)
    assert content.to_dict() == saved
    restored = [Message.from_dict(message.to_dict()) for message in messages]
    _assert_wire_text(restored, restored[1].contents[0])


def _assert_wire_text(messages: list[Message], content: Content) -> None:
    for client_type, key in [(OpenAIChatCompletionClient, "content"), (OpenAIChatClient, "output")]:
        client = client_type(model="test-model", api_key="unused")
        request = client._prepare_messages_for_openai(messages)
        result = next(
            item
            for item in request
            if item.get("tool_call_id", item.get("call_id")) == content.call_id and key in item
        )
        assert result[key] == content.result
        assert len(result[key]) < 1_000


async def test_recorded_row_removal_reaches_both_openai_request_formats():
    header = "| Quota | Limit | Usage |\n| --- | --- | --- |\n"
    rows = [f"| quota-{i:04d} | limit-{i:04d}-1000 | usage-{i:04d}-2000 |\n" for i in range(100)]
    text = header + "".join(rows)
    content = Content.from_function_result(call_id="lookup-1", result=text)
    messages = [Message("tool", [content])]
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=100_000, tokenizer=TOKENIZER
    )
    quoted = set(_table_rows(header + "".join(rows[:90])).values())

    assert strategy._release_recorded_rows(messages, quoted)
    assert rows[0] not in content.result
    assert rows[-1] in content.result
    _assert_wire_text(messages, content)
    assert not strategy._release_recorded_rows(messages, quoted)
