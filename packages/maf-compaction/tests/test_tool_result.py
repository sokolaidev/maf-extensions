"""Host-directed rewrites preserve labels, media and dry-run isolation."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from agent_framework import CharacterEstimatorTokenizer, Content, Message
from agent_framework._compaction import (
    annotate_message_groups,
    group_messages,
    included_token_count,
)

from maf_compaction import (
    AnchoredCompactionStrategy,
    MediaTokenEstimator,
    MinimumGainAnchoredCompactionStrategy,
    ToolResultAnchoredSummarizationCompactionStrategy,
)
from maf_compaction._tool_result import plan_result_items
from maf_compaction._toolsummary import _table_rows

TOKENIZER = CharacterEstimatorTokenizer()
HEADER = "| Quota | Limit | Usage |\n| --- | --- | --- |\n"
ROWS = [f"| quota-{i:04d} | limit-{i:04d}-1000 | usage-{i:04d}-2000 |\n" for i in range(100)]


def _merge_labels(content: Content, text: str) -> bool:
    items = content.items or []
    texts = [item for item in items if item.type == "text"]
    if not texts:
        return False
    replacement = texts[0]
    replacement.text = text
    labels = [item.additional_properties["security_label"] for item in texts]
    replacement.additional_properties["security_label"] = {
        "integrity": "untrusted"
        if any(p["integrity"] == "untrusted" for p in labels)
        else "trusted",
        "confidentiality": "private"
        if any(p["confidentiality"] == "private" for p in labels)
        else "public",
    }
    if replacement.raw_representation is not None:
        replacement.raw_representation = {"text": text}
    content.items = []
    for item in items:
        if item is texts[0]:
            content.items.append(replacement)
        elif item.type != "text":
            content.items.append(item)
    return True


def _result(shape: str) -> Content:
    item = Content.from_text(HEADER + "".join(ROWS))
    item.additional_properties["security_label"] = {
        "integrity": "untrusted",
        "confidentiality": "private",
    }
    item.additional_properties["provenance"] = {"sources": ["lookup"]}
    items = [item]
    if shape == "multiple":
        item.additional_properties["security_label"]["integrity"] = "trusted"
        second = Content.from_text("another result")
        second.additional_properties["security_label"] = {
            "integrity": "untrusted",
            "confidentiality": "public",
        }
        items.append(second)
    elif shape == "raw":
        item.raw_representation = {"text": item.text}
    elif shape == "media":
        items = [
            Content.from_uri("https://example.com/before.png", media_type="image/png"),
            item,
            Content.from_data(b"image", media_type="image/png"),
        ]
    result = Content.from_function_result(call_id="lookup-1", result=items)
    result.additional_properties["host_note"] = "retain"
    return result


def _messages(content: Content) -> list[Message]:
    return [
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup-1", name="lookup", arguments="{}")],
        ),
        Message("tool", [content]),
    ]


@pytest.mark.parametrize("shape", ["single", "raw", "media"])
@pytest.mark.parametrize("kind", ["anchored", "minimum_gain", "recorded_rows"])
def test_unconfigured_rewrites_skip_processing_ineligible_results(shape, kind):
    class UnparsedText(str):
        def splitlines(self, keepends: bool = False) -> list[str]:
            raise AssertionError("ineligible result was split into rows")

    content = _result(shape)
    content.result = UnparsedText(content.result)
    if shape != "single":
        for item in content.items or []:
            item.additional_properties.clear()
    messages = _messages(content)
    original = content.to_dict()
    original_items = content.items
    annotate_message_groups(messages)
    groups = group_messages(messages)
    quoted = set(_table_rows(HEADER + "".join(ROWS[:90])).values())
    with (
        patch.object(
            TOKENIZER, "count_tokens", side_effect=AssertionError("ineligible text counted")
        ),
        patch(
            "maf_compaction._toolsummary._table_rows",
            side_effect=AssertionError("ineligible table parsed"),
        ),
    ):
        if kind == "recorded_rows":
            record = ToolResultAnchoredSummarizationCompactionStrategy(
                max_input_tokens=100_000, tokenizer=TOKENIZER
            )
            for _ in range(2):
                assert not record._release_recorded_rows(messages, quoted)
        else:
            strategy_type = (
                AnchoredCompactionStrategy
                if kind == "anchored"
                else MinimumGainAnchoredCompactionStrategy
            )
            strategy = strategy_type(max_input_tokens=100_000, tokenizer=TOKENIZER, keep_tokens=100)
            for _ in range(2):
                assert strategy._plan_shortenings(messages, groups) == []
    assert content.to_dict() == original
    assert content.items is original_items


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["single", "multiple", "raw", "media"])
@pytest.mark.parametrize("kind", ["anchored", "minimum_gain", "recorded_rows"])
async def test_rewrite_retains_metadata_media_and_replays(shape, kind):
    content = _result(shape)
    original = content.result
    media = [item for item in content.items or [] if item.type != "text"]
    messages = _messages(content)
    quoted = set(_table_rows(HEADER + "".join(ROWS[:90])).values())
    if kind == "recorded_rows":
        strategy = ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=100_000, tokenizer=TOKENIZER, rewrite_result_text=_merge_labels
        )
        assert strategy._release_recorded_rows(messages, quoted)
        assert ROWS[0] not in content.result
        assert ROWS[-1] in content.result
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
            rewrite_result_text=_merge_labels,
        )
        assert await strategy(messages)
    assert len(content.result) < len(original)
    assert content.additional_properties == {"host_note": "retain"}
    assert content.call_id == "lookup-1"
    texts = [item for item in content.items or [] if item.type == "text"]
    assert len(texts) == 1
    assert texts[0].text == content.result
    assert texts[0].additional_properties == {
        "security_label": {"integrity": "untrusted", "confidentiality": "private"},
        "provenance": {"sources": ["lookup"]},
    }
    assert [item for item in content.items or [] if item.type != "text"] == media
    if media:
        assert content.items
        assert content.items[0] is media[0]
        assert content.items[-1] is media[-1]
    if shape == "raw":
        assert texts[0].raw_representation == {"text": content.result}
    restored = [Message.from_dict(message.to_dict()) for message in messages]
    before = restored[1].contents[0].to_dict()
    if isinstance(strategy, ToolResultAnchoredSummarizationCompactionStrategy):
        assert not strategy._release_recorded_rows(restored, quoted)
    else:
        assert not await strategy(restored)
    assert restored[1].contents[0].to_dict() == before


@pytest.mark.parametrize(
    "failure", ["refuse", "raise", "wrong_text", "drop_media", "reorder_media"]
)
def test_failed_hook_leaves_original_items_and_nested_metadata_untouched(failure):
    content = _result("media")
    original_items = content.items
    original = deepcopy(content.to_dict())

    def rewrite(candidate: Content, text: str) -> bool:
        assert candidate.items
        candidate.items[1].additional_properties["provenance"]["sources"].append("changed")
        candidate.items[1].text = text
        if failure == "raise":
            raise RuntimeError("hook failed")
        if failure == "wrong_text":
            candidate.items[1].text = "wrong"
        if failure == "drop_media":
            candidate.items = candidate.items[1:]
        if failure == "reorder_media":
            candidate.items = list(reversed(candidate.items))
        return failure != "refuse"

    if failure == "refuse":
        assert plan_result_items(content, "shortened", rewrite) is None
    else:
        with pytest.raises(RuntimeError if failure == "raise" else ValueError):
            plan_result_items(content, "shortened", rewrite)
    assert content.to_dict() == original
    assert content.items is original_items


@pytest.mark.anyio
async def test_minimum_gain_decline_keeps_hook_changes_out_of_history():
    content = _result("single")
    messages = _messages(content)
    messages.append(Message("user", ["tail " * 10_000]))
    before = deepcopy(content.to_dict())
    calls = []

    def rewrite(candidate: Content, text: str) -> bool:
        calls.append(text)
        return _merge_labels(candidate, text)

    strategy = MinimumGainAnchoredCompactionStrategy(
        max_input_tokens=100_000,
        tokenizer=TOKENIZER,
        keep_head_groups=0,
        keep_tail_groups=1,
        keep_tokens=100,
        rewrite_result_text=rewrite,
    )
    assert not await strategy(messages)
    assert len(calls) == 1
    assert strategy.declined_collapses == 1
    assert content.to_dict() == before


@pytest.mark.anyio
async def test_default_record_fallback_uses_hook_and_counts_retained_media():
    content = _result("media")
    messages = _messages(content)
    estimator = MediaTokenEstimator(TOKENIZER, image_tokens=100)
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=1_000,
        tokenizer=estimator,
        keep_head_groups=0,
        keep_tail_groups=0,
        rewrite_result_text=_merge_labels,
    )
    assert await strategy(messages)
    assert strategy.fallbacks_used == 1
    assert len([item for item in content.items or [] if item.type != "text"]) == 2
    assert 200 < included_token_count(messages) < 1_000
    assert estimator.count_message_tokens(messages[1]) > 200


def test_explicit_fallback_keeps_its_own_hook():
    fallback = AnchoredCompactionStrategy(max_input_tokens=1_000, tokenizer=TOKENIZER)
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        fallback=fallback,
        rewrite_result_text=_merge_labels,
    )
    assert strategy.fallback is fallback
    assert fallback.rewrite_result_text is None


def test_bare_text_uses_builtin_rewrite_without_calling_hook():
    def unexpected(content: Content, text: str) -> bool:
        pytest.fail("bare text must not call the hook")

    content = Content.from_function_result(call_id="lookup-1", result="original")
    items = plan_result_items(content, "short", unexpected)
    assert items and items[0].text == "short"


def test_hook_cannot_change_original_media_or_outer_metadata():
    content = _result("media")
    assert content.items
    media = content.items[0]
    media.raw_representation = {"provider": ["original"]}

    def rewrite(candidate: Content, text: str) -> bool:
        assert candidate.items
        candidate.items[0].uri = "https://example.com/changed.png"
        assert candidate.items[0].raw_representation is not None
        candidate.items[0].raw_representation["provider"].append("changed")
        candidate.additional_properties["host_note"] = "changed"
        return _merge_labels(candidate, text)

    items = plan_result_items(content, "short", rewrite)
    assert items and items[0] is media
    assert media.uri == "https://example.com/before.png"
    assert media.raw_representation == {"provider": ["original"]}
    assert content.additional_properties == {"host_note": "retain"}


def test_refused_hook_cannot_mutate_source_provider_state():
    content = _result("raw")
    assert content.items
    raw = content.items[0].raw_representation
    before = deepcopy(raw)

    def refuse(candidate: Content, text: str) -> bool:
        assert candidate.items
        assert candidate.items[0].raw_representation is not None
        candidate.items[0].raw_representation["text"] = text
        return False

    assert plan_result_items(content, "short", refuse) is None
    assert raw == before
    assert content.items[0].raw_representation is raw
