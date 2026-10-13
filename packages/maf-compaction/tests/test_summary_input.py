"""Whole-unit request budgets, partial replacement, and replay."""

import json
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

import maf_compaction._composed as composed_module
import maf_compaction._usersummary as users_module
from maf_compaction import (
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    UserTurnAnchoredSummarizationCompactionStrategy,
)
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


def summaries(count=5, body=None):
    return [
        UserTurnAnchoredSummarizationCompactionStrategy._summary_message(
            [Message("user", ["source"], message_id=f"source{i}")],
            [{"group_id": f"group{i}"}],
            body if body is not None else f"Summary {i}: " + "s" * 1_500,
            summary_id=f"user_summary_{i}",
        )
        for i in range(count)
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


@pytest.mark.parametrize("ordinary", [False, True])
async def test_a_fold_needs_two_whole_summaries_to_fit(ordinary):
    client = BoundedClient(500)
    strategy = users(client, max_summary_input_tokens=500, summary_mode="fold", min_band_share=0)
    messages = summaries()
    before = visible(messages)
    size(messages)
    if ordinary:
        assert not await strategy(messages)
        assert strategy.user_passes_declined == 1
    else:
        assert await strategy.fold_if_smaller(messages) == "skipped"
    assert not client.requests
    assert visible(messages) == before


@pytest.mark.parametrize(
    ("ordinary", "share", "folded"),
    [(True, 0.1, False), (True, 0.01, True), (False, 0.1, True)],
)
async def test_partial_fold_uses_only_selected_summaries_for_profitability(ordinary, share, folded):
    client = BoundedClient(1_000)
    strategy = users(client, summary_mode="fold", min_band_share=share)
    messages = [*summaries(), Message("user", ["tail " * 2_000], message_id="tail")]
    size(messages)
    standing = strategy._observe_summaries(messages)
    assert strategy._fold_due(messages, standing)
    selected = strategy._bounded_summaries(messages, standing)
    assert len(selected) == 2
    assert strategy._fold_due(messages, selected) is (share < 0.1)
    before = visible(messages)
    stored = deepcopy(messages)

    if ordinary:
        assert await strategy(messages) is folded
        assert strategy.user_passes_declined == int(not folded)
    else:
        assert await strategy.fold_if_smaller(messages) == "folded"
    assert strategy.user_folds == int(folded)
    assert len(client.requests) == int(folded)
    if folded:
        assert "Summary 2:" not in client.requests[0][-1].text
        if ordinary:
            assert await strategy(stored)
        else:
            assert strategy.refold(stored)
        assert visible(stored) == visible(messages)
        assert len(client.requests) == 1
    else:
        assert visible(messages) == before


@pytest.mark.parametrize(
    ("mode", "limit", "count", "folded"),
    [
        ("fold", 1_000, 2, True),
        ("boundary", 1_000, 2, False),
        ("fold", 500, 2, False),
        ("fold", 1_000, 1, False),
    ],
)
async def test_oversized_user_band_can_fall_back_to_an_eligible_fold(mode, limit, count, folded):
    client = BoundedClient(limit)
    strategy = users(client, summary_mode=mode, max_summary_input_tokens=limit, min_band_share=0.1)
    oversized = Message("user", ["new turn " * 1_000], message_id="oversized")
    messages = [
        Message("user", ["head"], message_id="head"),
        *summaries(count=count),
        oversized,
        Message("user", ["tail"], message_id="tail"),
    ]
    stored = deepcopy(messages)
    before = visible(messages)
    assert await strategy(messages) is folded
    assert not oversized.additional_properties.get(EXCLUDED_KEY)
    assert strategy.user_compactions == 0
    if folded:
        assert len(client.requests) == 1
        assert client.requests[0][0].text == strategy.fold_prompt
        assert strategy.user_folds == 1
        assert strategy.user_passes_declined == 0
        assert await strategy(stored)
        assert visible(stored) == visible(messages)
        assert strategy.user_summaries_replayed == 1
        assert len(client.requests) == 1
    else:
        assert not client.requests
        assert visible(messages) == before
        assert strategy.user_passes_declined == 1


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
    stored = deepcopy(messages)
    newer = messages[1:]
    assert not await strategy._merge_records(messages)
    assert await strategy._rewrite_records(messages, composed_module._ChainStart.take(messages))
    assert len(client.requests) == 1
    assert "Record 0:" in client.requests[0][-1].text
    assert "Record 1:" not in client.requests[0][-1].text
    assert all(not m.additional_properties.get(EXCLUDED_KEY) for m in newer)
    restored = composed(client, harder_attempts=1)
    restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert restored._keep_records(stored)
    assert visible(stored) == visible(messages)
    assert set(restored.session_state()["kept_record_counts"].values()) == {1}
    assert len(client.requests) == 1


async def test_oversized_record_is_retained_without_any_summary_request():
    client = BoundedClient(100)
    strategy = composed(client)
    messages = records()
    before = visible(messages)
    assert not await strategy._merge_records(messages)
    assert not await strategy._rewrite_records(messages, composed_module._ChainStart.take(messages))
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


def test_nonmonotonic_tokenizer_finds_a_prefix_after_an_oversized_first_item():
    class NonmonotonicTokenizer:
        def count_tokens(self, text):
            body = json.loads(text)["contents"][0]["text"]
            return 1 if body in {"instructions", "abc"} else 100

    assert bounded_prefix(
        list("abcd"),
        render="".join,
        prompt="instructions",
        tokenizer=NonmonotonicTokenizer(),
        limit=10,
    ) == list("abc")


@pytest.mark.parametrize("kind", ["fold", "merge", "rewrite", "recompact"])
async def test_nonmonotonic_counts_respect_each_operations_minimum_prefix(kind):
    class NonmonotonicTokenizer:
        def count_tokens(self, text):
            payload = json.loads(text)
            body = payload["contents"][0]["text"]
            if payload["role"] == "system":
                return 1
            if body.startswith("1. "):
                fitting = {3} if kind == "rewrite" else {1, 3}
                return 10 if len(body.splitlines()) in fitting else 10_000
            return TOKENIZER.count_tokens(text)

    client = BoundedClient(10_000)
    strategy = users(client, max_summary_input_tokens=1_000, min_band_share=0)
    strategy.tokenizer = NonmonotonicTokenizer()
    if kind == "fold":
        messages = summaries()
        size(messages)
        assert await strategy.fold_if_smaller(messages) == "folded"
    elif kind == "recompact":
        messages = conversation()
        messages[1] = summaries()[0]
        assert await strategy(messages)
    else:
        chain = composed(client)
        chain.user_turns = strategy
        chain.tokenizer = strategy.tokenizer
        messages = records()
        if kind == "merge":
            assert await chain._merge_records(messages)
        else:
            assert await chain._rewrite_records(
                messages, composed_module._ChainStart.take(messages)
            )
    assert len(client.requests) == 1
    assert len(client.requests[0][-1].text.splitlines()) == 3
    annotate_token_counts(client.requests[0], tokenizer=strategy.tokenizer, force_retokenize=True)
    assert included_token_count(client.requests[0]) <= 1_000


@pytest.mark.parametrize("mode", ["boundary", "fold"])
@pytest.mark.parametrize("remembered_requests", [1, 8])
async def test_identical_user_chunks_get_distinct_ids_and_replay_the_latest(
    mode, remembered_requests
):
    client = BoundedClient(1_000)
    strategy = users(
        client, summary_mode=mode, min_band_share=0, remembered_requests=remembered_requests
    )
    messages = [Message("user", ["u" * 1_500], message_id=f"u{i}") for i in range(8)]
    stored = deepcopy(messages)
    for index in range(3):
        assert await strategy(messages)
        ids = [message.message_id for message in messages]
        assert len(ids) == len(set(ids))
        assert (strategy.user_compactions, strategy.user_summaries_replayed) == (index + 1, index)
        assert await strategy(stored)
        assert visible(stored) == visible(messages)
    assert strategy.user_summaries_replayed == 3
    assert len(client.requests) == 1


async def test_refold_never_uses_its_own_output_as_a_source(monkeypatch):
    body = "same summary " * 100
    client = BoundedClient(2_000, body)
    strategy = users(client, max_summary_input_tokens=2_000, summary_mode="fold")

    def summary(index):
        return strategy._summary_message(
            [Message("user", ["source"], message_id=f"source{index}")],
            [{"group_id": f"source_group{index}"}],
            body,
            summary_id=f"user_summary_{index}",
        )

    messages = [summary(0), summary(1)]
    size(messages)
    assert await strategy.fold_if_smaller(messages) == "folded"
    messages.append(summary(2))
    size(messages)
    before = visible(messages)

    def reject_self_reference(*args):
        pytest.fail("a standing fold output must not be replayed over itself")

    monkeypatch.setattr(strategy, "_apply_fold", reject_self_reference)
    assert not strategy.refold(messages)
    assert visible(messages) == before
    assert len(client.requests) == 1


@pytest.mark.parametrize("remembered_requests", [1, 32])
async def test_repeated_partial_folds_with_identical_bodies_replay_in_order(remembered_requests):
    body = "same summary " * 100
    client = BoundedClient(1_000, body)
    strategy = users(
        client,
        max_summary_input_tokens=1_000,
        summary_mode="fold",
        remembered_requests=remembered_requests,
    )
    messages = summaries(count=15, body=body)
    size(messages)
    stored = deepcopy(messages)
    for _ in range(12):
        assert await strategy.fold_if_smaller(messages) == "folded"
    assert len(visible(messages)) == 3
    assert (strategy.user_folds, strategy.user_summaries_replayed) == (12, 0)
    restored = users(client, max_summary_input_tokens=1_000, summary_mode="fold")
    restored.restore_session_state(json.loads(json.dumps(strategy.session_state(), sort_keys=True)))
    assert restored.refold(stored)
    assert visible(stored) == visible(messages)
    assert not restored.refold(stored)
    assert not restored.refold(project_included_messages(stored))
    assert len(client.requests) == 1


async def test_identical_fold_requests_replay_the_latest_id_with_a_larger_memory():
    body = "same summary " * 100
    client = BoundedClient(1_000, body)
    strategy = users(
        client, max_summary_input_tokens=1_000, summary_mode="fold", remembered_requests=8
    )
    messages = summaries(body=body)
    size(messages)
    stored = deepcopy(messages)
    for _ in range(3):
        assert await strategy.fold_if_smaller(messages) == "folded"
        assert await strategy.fold_if_smaller(stored) == "folded"
        assert visible(stored) == visible(messages)
    assert (strategy.user_folds, strategy.user_summaries_replayed) == (3, 3)
    assert len(client.requests) == 1


@pytest.mark.parametrize("kind", ["fold", "record"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_partial_replay_renders_only_saved_prefix_sizes(kind, legacy, monkeypatch):
    body = "same summary " * 100
    client = BoundedClient(1_000, body)
    strategy = users(client, summary_mode="fold") if kind == "fold" else composed(client)
    messages = (
        summaries(count=48, body=body)
        if kind == "fold"
        else [build_record_message(f"Record {index}: {body}") for index in range(48)]
    )
    annotate_message_groups(messages)
    size(messages)
    stored = deepcopy(messages)
    original_history = deepcopy(messages)
    for _ in range(32):
        if isinstance(strategy, UserTurnAnchoredSummarizationCompactionStrategy):
            assert await strategy.fold_if_smaller(messages) == "folded"
        else:
            assert await strategy._merge_records(messages)
    assert len(visible(messages)) == 16
    snapshot = json.loads(json.dumps(strategy.session_state(), sort_keys=True))
    count_key = "kept_fold_counts" if kind == "fold" else "kept_record_counts"
    if legacy:
        snapshot.pop(count_key, None)
    restored = users(client, summary_mode="fold") if kind == "fold" else composed(client)
    restored.restore_session_state(snapshot)
    rendered = []
    module = users_module if kind == "fold" else composed_module
    name = "_format_turns" if kind == "fold" else "_transcript"
    original = getattr(module, name)

    def counted(*args, **kwargs):
        rendered.append(len(args[0] if kind == "fold" else args[1]))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    requests = len(client.requests)
    replay = (
        restored.refold
        if isinstance(restored, UserTurnAnchoredSummarizationCompactionStrategy)
        else restored._keep_records
    )
    assert replay(stored)
    assert visible(stored) == visible(messages)
    if not legacy:
        assert max(rendered) == 2
        assert len(rendered) <= 33
    assert not replay(stored)
    assert len(client.requests) == requests
    assert set(restored.session_state()[count_key].values()) == {2}
    rendered.clear()
    assert replay(original_history)
    assert visible(original_history) == visible(messages)
    assert max(rendered) == 2
    assert len(rendered) <= 33
