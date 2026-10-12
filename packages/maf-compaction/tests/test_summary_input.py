"""Whole-unit request budgets, partial replacement, and replay."""

from copy import deepcopy

import pytest
from agent_framework import CharacterEstimatorTokenizer, ChatResponse, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
    project_included_messages,
)

from maf_compaction import (
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    UserTurnAnchoredSummarizationCompactionStrategy,
)
from maf_compaction._composed import _ChainStart
from maf_compaction._summary_input import bounded_prefix
from maf_compaction._toolsummary import active_record_groups, build_record_message

pytestmark = pytest.mark.anyio
TOKENIZER = CharacterEstimatorTokenizer()


def size(messages):
    annotate_token_counts(messages, tokenizer=TOKENIZER, force_retokenize=True)
    return included_token_count(messages)


class BoundedClient:
    def __init__(self, limit, answer="Earlier facts."):
        self.limit = limit
        self.answer = answer
        self.requests = []

    async def get_response(self, messages, **kwargs):
        assert size(messages) <= self.limit
        self.requests.append(messages)
        if isinstance(self.answer, Exception):
            raise self.answer
        return ChatResponse(messages=Message("assistant", [self.answer]))


def users(client, **kwargs):
    kwargs.setdefault("max_input_tokens", 2_000)
    kwargs.setdefault("trigger_fraction", 0.01)
    return UserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=TOKENIZER, client=client, **kwargs
    )


def conversation():
    return [
        Message("user", [f"Turn {index}: " + "u" * 1_500], message_id=f"u{index}")
        for index in range(8)
    ]


def visible(messages):
    return [(m.message_id, m.text) for m in project_included_messages(messages)]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limit(limit):
    with pytest.raises(ValueError, match="max_summary_input_tokens"):
        users(BoundedClient(1_000), max_summary_input_tokens=limit)


def test_default_is_half_the_input_ceiling_with_a_positive_minimum():
    assert users(BoundedClient(1_000)).max_summary_input_tokens == 1_000
    assert users(BoundedClient(1), max_input_tokens=1).max_summary_input_tokens == 1


@pytest.mark.parametrize("mode", ["recompact", "boundary", "fold"])
async def test_default_bounds_oldest_turns_and_replays_then_advances(mode):
    client = BoundedClient(1_000)
    strategy = users(client, summary_mode=mode)
    loaded = conversation()
    stored = deepcopy(loaded)
    originals = list(loaded)

    assert await strategy(loaded)
    assert len(client.requests) == 1
    transcript = client.requests[0][-1].text
    assert "Turn 1:" in transcript and "Turn 3:" not in transcript
    assert all(m.additional_properties.get(EXCLUDED_KEY) for m in originals[1:3])
    assert all(not m.additional_properties.get(EXCLUDED_KEY) for m in originals[3:])
    assert not originals[0].additional_properties.get(EXCLUDED_KEY)
    assert await strategy(stored)
    assert len(client.requests) == 1
    assert visible(stored) == visible(loaded)

    assert await strategy(loaded)
    assert "Turn 3:" in client.requests[-1][-1].text
    assert "Turn 7:" not in client.requests[-1][-1].text


async def test_eligible_large_band_can_make_progress_below_the_share_in_one_batch():
    client = BoundedClient(1_000)
    strategy = users(client)
    messages = conversation()
    messages.extend(
        Message("user", [f"Extra {i}: " + "u" * 1_500], message_id=f"extra{i}") for i in range(50)
    )
    assert size(messages) * strategy.min_band_share > client.limit
    assert await strategy(messages)
    assert len(client.requests) == 1


@pytest.mark.parametrize("obstacle", ["turn", "prompt", "summary_only"])
async def test_unfittable_oldest_unit_never_skips_ahead(obstacle):
    client = BoundedClient(1_000)
    strategy = users(client, min_band_share=0)
    messages = conversation()
    if obstacle == "turn":
        messages[1] = Message("user", ["x" * 8_000], message_id="large")
    elif obstacle == "prompt":
        strategy.prompt = "x" * 8_000
    else:
        messages[1] = strategy._summary_message(
            [messages[1]], [{"group_id": "old"}], "s" * 3_000, summary_id="user_summary_0"
        )
    before = visible(messages)
    assert not await strategy(messages)
    assert not client.requests
    assert visible(messages) == before


@pytest.mark.parametrize("answer", [RuntimeError("offline"), " ", "x" * 8_000])
async def test_failed_or_nonshrinking_partial_band_preserves_every_turn(answer):
    client = BoundedClient(1_000, answer)
    strategy = users(client)
    messages = conversation()
    before = visible(messages)
    assert not await strategy(messages)
    assert len(client.requests) == 1
    assert visible(messages) == before


def summaries():
    return [
        UserTurnAnchoredSummarizationCompactionStrategy._summary_message(
            [Message("user", ["source"], message_id=f"source{i}")],
            [{"group_id": f"group{i}"}],
            f"Summary {i}: " + "s" * 1_500,
            summary_id=f"user_summary_{i}",
        )
        for i in range(5)
    ]


@pytest.mark.parametrize("ordinary", [False, True])
async def test_partial_fold_preserves_newer_summaries_and_replays(ordinary):
    client = BoundedClient(1_000)
    strategy = users(client, summary_mode="fold", min_band_share=0)
    messages = summaries()
    size(messages)
    stored = deepcopy(messages)
    newer = messages[2:]
    if ordinary:
        assert await strategy(messages)
        assert await strategy(stored)
    else:
        assert await strategy.fold_if_smaller(messages) == "folded"
        restored = users(client, summary_mode="fold")
        restored.restore_session_state(strategy.session_state())
        assert restored.refold(stored)
    assert len(client.requests) == 1
    assert all(not m.additional_properties.get(EXCLUDED_KEY) for m in newer)
    assert all(m in messages for m in newer)
    assert visible(stored) == visible(messages)


async def test_a_fold_needs_two_whole_summaries_to_fit():
    client = BoundedClient(500)
    strategy = users(client, max_summary_input_tokens=500)
    messages = summaries()
    before = visible(messages)
    size(messages)
    assert await strategy.fold_if_smaller(messages) == "skipped"
    assert not client.requests
    assert visible(messages) == before


def composed(client, **kwargs):
    return ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=TOKENIZER,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=2_000, tokenizer=TOKENIZER
        ),
        user_turns=users(client, max_summary_input_tokens=client.limit),
        **kwargs,
    )


def records():
    messages = [build_record_message(f"Record {i}: " + "r" * 1_500) for i in range(5)]
    annotate_message_groups(messages)
    size(messages)
    return messages


async def test_record_merge_replaces_only_the_bounded_prefix_and_replays():
    client = BoundedClient(1_000)
    strategy = composed(client)
    messages = records()
    stored = deepcopy(messages)
    newer = messages[2:]
    assert await strategy._merge_records(messages)
    assert len(active_record_groups(messages)) == 4
    assert "Record 0:" in client.requests[0][-1].text
    assert "Record 2:" not in client.requests[0][-1].text
    assert all(not m.additional_properties.get(EXCLUDED_KEY) for m in newer)
    assert strategy._keep_records(stored)
    assert visible(stored) == visible(messages)
    assert len(client.requests) == 1


async def test_harder_rewrite_bounds_the_request_when_merge_cannot_fit_two():
    client = BoundedClient(650)
    strategy = composed(client, harder_attempts=1)
    messages = records()
    newer = messages[1:]
    assert not await strategy._merge_records(messages)
    assert await strategy._rewrite_records(messages, _ChainStart.take(messages))
    assert len(client.requests) == 1
    assert "Record 0:" in client.requests[0][-1].text
    assert "Record 1:" not in client.requests[0][-1].text
    assert all(not m.additional_properties.get(EXCLUDED_KEY) for m in newer)


async def test_oversized_record_is_retained_without_any_summary_request():
    client = BoundedClient(100)
    strategy = composed(client)
    messages = records()
    before = visible(messages)
    assert not await strategy._merge_records(messages)
    assert not await strategy._rewrite_records(messages, _ChainStart.take(messages))
    assert not client.requests
    assert visible(messages) == before


@pytest.mark.parametrize("answer", [RuntimeError("offline"), " ", "x" * 8_000])
async def test_failed_or_nonshrinking_record_merge_keeps_every_record(answer):
    client = BoundedClient(1_000, answer)
    strategy = composed(client)
    messages = records()
    before = visible(messages)
    assert not await strategy._merge_records(messages)
    assert len(client.requests) == 1
    assert visible(messages) == before


def test_large_prefix_selection_avoids_quadratic_tokenization():
    class CountingTokenizer:
        def __init__(self):
            self.calls = 0

        def count_tokens(self, text):
            self.calls += 1
            return len(text)

    tokenizer = CountingTokenizer()
    items = ["x" * 100] * 10_000
    selected = bounded_prefix(
        items, render="\n".join, prompt="instructions", tokenizer=tokenizer, limit=400_000
    )
    assert 3_900 < len(selected) < 4_000
    assert tokenizer.calls < 100
    assert len("instructions") + len("\n".join(selected)) <= 400_000
