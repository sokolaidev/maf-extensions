"""Compacted tool results must agree with the provider request payload."""

from __future__ import annotations

import json

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


@pytest.mark.parametrize("media", [False, True])
@pytest.mark.parametrize("kind", ["anchored", "minimum_gain", "recorded_rows"])
async def test_host_rewrite_reaches_provider_requests_with_media_and_labels(media, kind):
    header = "| Quota | Limit | Usage |\n| --- | --- | --- |\n"
    rows = [f"| quota-{i:04d} | limit-{i:04d}-1000 | usage-{i:04d}-2000 |\n" for i in range(100)]
    item = Content.from_text(header + "".join(rows))
    item.additional_properties["security_label"] = {"integrity": "untrusted"}
    items = [item]
    if media:
        items.append(Content.from_uri("https://example.com/result.png", media_type="image/png"))
    content = Content.from_function_result(call_id="lookup-1", result=items)
    messages = [
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup-1", name="lookup", arguments="{}")],
        ),
        Message("tool", [content]),
    ]

    def rewrite(candidate: Content, text: str) -> bool:
        assert candidate.items
        candidate.items[0].text = text
        return True

    if kind == "recorded_rows":
        record = ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=100_000, tokenizer=TOKENIZER, rewrite_result_text=rewrite
        )
        assert record._release_recorded_rows(
            messages, set(_table_rows(header + "".join(rows[:90])).values())
        )
    else:
        strategy_type = (
            AnchoredCompactionStrategy
            if kind == "anchored"
            else MinimumGainAnchoredCompactionStrategy
        )
        strategy = strategy_type(
            max_input_tokens=100_000,
            tokenizer=TOKENIZER,
            keep_head_groups=0,
            keep_tail_groups=0,
            keep_tokens=100,
            rewrite_result_text=rewrite,
        )
        assert await strategy(messages)
    for history in [messages, [Message.from_dict(message.to_dict()) for message in messages]]:
        for client_type in [OpenAIChatClient, OpenAIChatCompletionClient]:
            client = client_type(model="test-model", api_key="unused")
            request = json.dumps(client._prepare_messages_for_openai(history))
            assert json.dumps(content.result)[1:-1] in request
            assert "quota-0050" not in request
            if media and client_type is OpenAIChatClient:
                assert "https://example.com/result.png" in request
        assert history[1].contents[0].items
        assert history[1].contents[0].items[0].additional_properties == {
            "security_label": {"integrity": "untrusted"}
        }


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


@pytest.mark.parametrize("strategy_kind", ["anchored", "minimum_gain", "recorded_rows"])
@pytest.mark.parametrize("metadata", ["label", "custom", "annotation", "raw", "id"])
@pytest.mark.parametrize("shape", ["single", "bare_prefix", "labelled_prefix"])
async def test_shortening_preserves_metadata_bearing_text_items(strategy_kind, metadata, shape):
    header = "| Quota | Limit | Usage |\n| --- | --- | --- |\n"
    rows = [f"| quota-{i:04d} | limit-{i:04d}-1000 | usage-{i:04d}-2000 |\n" for i in range(100)]
    text = header + "".join(rows)
    item = Content.from_text(text)
    if metadata == "label":
        item.additional_properties["security_label"] = {
            "integrity": "untrusted",
            "confidentiality": "private",
        }
    elif metadata == "custom":
        item.additional_properties["host_provenance"] = {"source": "tool"}
    elif metadata == "annotation":
        item = Content.from_text(text, annotations=[{"type": "citation", "title": "source"}])
    elif metadata == "raw":
        item.raw_representation = object()
    else:
        item.id = "provider-item-1"
    items = [item]
    if shape != "single":
        prefix = Content.from_text("heading")
        if shape == "labelled_prefix":
            prefix.additional_properties["security_label"] = {
                "integrity": "trusted",
                "confidentiality": "public",
            }
        items.insert(0, prefix)
    content = Content.from_function_result(call_id="lookup-1", result=items)
    messages = [
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup-1", name="lookup", arguments="{}")],
        ),
        Message("tool", [content]),
    ]
    original = content.to_dict()
    original_items = content.items
    original_raw = item.raw_representation

    if strategy_kind == "recorded_rows":
        strategy = ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=100_000, tokenizer=TOKENIZER
        )
        quoted = set(_table_rows(header + "".join(rows[:90])).values())
        assert not strategy._release_recorded_rows(messages, quoted)
    else:
        strategy_type = (
            AnchoredCompactionStrategy
            if strategy_kind == "anchored"
            else MinimumGainAnchoredCompactionStrategy
        )
        anchored = strategy_type(
            max_input_tokens=100_000,
            tokenizer=TOKENIZER,
            keep_head_groups=0,
            keep_tail_groups=0,
            keep_tokens=100,
        )
        assert not await anchored(messages)

    assert content.to_dict() == original
    assert content.items is original_items
    assert item.raw_representation is original_raw


@pytest.mark.parametrize(
    "strategy_type", [AnchoredCompactionStrategy, MinimumGainAnchoredCompactionStrategy]
)
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("metadata", [False, True])
async def test_reloaded_anchored_result_repairs_stale_items_below_threshold(
    strategy_type, protected, metadata
):
    original = "head " + "original-middle " * 2_000 + " tail"
    strategy = strategy_type(
        max_input_tokens=100_000,
        tokenizer=TOKENIZER,
        keep_head_groups=1 if protected else 0,
        keep_tail_groups=0,
        keep_tokens=100,
    )
    content = Content.from_function_result(call_id="lookup-1", result=original)
    content.result = strategy._shorten(original, 100)
    if metadata:
        assert content.items
        content.items[0].additional_properties["security_label"] = {"integrity": "untrusted"}
    messages = [
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup-1", name="lookup", arguments="{}")],
        ),
        Message("tool", [content]),
    ]
    restored = [Message.from_dict(message.to_dict()) for message in messages]
    expected = content.result

    original_content = restored[1].contents[0].to_dict()
    assert (await strategy(restored)) is not metadata
    assert restored[1].contents[0].result == expected
    if metadata:
        assert restored[1].contents[0].to_dict() == original_content
    else:
        _assert_wire_text(restored, restored[1].contents[0])
    assert not await strategy(restored)


@pytest.mark.parametrize("entry", ["public", "release"])
@pytest.mark.parametrize("metadata", [False, True])
async def test_reloaded_recorded_rows_repair_without_new_removal(entry, metadata):
    header = "| Quota | Limit | Usage |\n| --- | --- | --- |\n"
    rows = [f"| quota-{i:04d} | limit-{i:04d}-1000 | usage-{i:04d}-2000 |\n" for i in range(100)]
    content = Content.from_function_result(call_id="lookup-1", result=header + "".join(rows))
    content.result = (
        "[Rows quoted verbatim in compaction records are omitted below.]\n"
        + header
        + "".join(rows[90:])
    )
    if metadata:
        assert content.items
        content.items[0].additional_properties["security_label"] = {"integrity": "untrusted"}
    messages = [Message.from_dict(Message("tool", [content]).to_dict())]
    original = messages[0].contents[0].to_dict()
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=100_000, tokenizer=TOKENIZER
    )
    changed = (
        await strategy(messages)
        if entry == "public"
        else strategy._release_recorded_rows(messages, set())
    )

    assert changed is not metadata
    if metadata:
        assert messages[0].contents[0].to_dict() == original
    else:
        assert messages[0].contents[0].result == content.result
        _assert_wire_text(messages, messages[0].contents[0])
    assert not await strategy(messages)
    assert not strategy._release_recorded_rows(messages, set())


@pytest.mark.parametrize(
    "strategy_type",
    [
        AnchoredCompactionStrategy,
        MinimumGainAnchoredCompactionStrategy,
        ToolResultAnchoredSummarizationCompactionStrategy,
    ],
)
async def test_unmarked_result_items_divergence_is_not_repaired(strategy_type):
    content = Content.from_function_result(call_id="lookup-1", result="provider text")
    content.result = "compatibility text"
    messages = [Message("tool", [content])]
    original = content.to_dict()
    strategy = strategy_type(max_input_tokens=100_000, tokenizer=TOKENIZER)

    assert not await strategy(messages)
    assert content.to_dict() == original
