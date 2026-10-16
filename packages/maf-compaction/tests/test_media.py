"""Media retention, accounting and replay through real compaction strategies."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from agent_framework import CharacterEstimatorTokenizer, ChatContext, ChatResponse, Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    included_token_count,
    project_included_messages,
)
from agent_framework._compaction import (
    annotate_token_counts as framework_counts,
)

from maf_compaction import (
    CompactionBudgetExceeded,
    CompactionStack,
    MediaTokenEstimator,
    RecallGate,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    UserTurnAnchoredSummarizationCompactionStrategy,
    is_preserved,
    set_preserved,
)
from maf_compaction._media import annotate_token_counts
from maf_compaction._summary_input import fits_summary_input

pytestmark = pytest.mark.anyio
BASE = CharacterEstimatorTokenizer()


def image(name="a"):
    return Content.from_uri(f"https://example.org/{name}.png", media_type="image/png")


def history(parts=None):
    return [
        Message("user", ["head"], message_id="head"),
        Message(
            "user", parts if parts is not None else ["Explain this.", image()], message_id="media"
        ),
        Message("user", ["tail"], message_id="tail"),
    ]


class Summarizer:
    def __init__(self, answer="The user asked about the diagram."):
        self.answer = answer
        self.requests = []

    async def get_response(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        if isinstance(self.answer, Exception):
            raise self.answer
        return ChatResponse(messages=[Message("assistant", [self.answer])])


def users(client=None, *, tokenizer=BASE, **kwargs):
    return UserTurnAnchoredSummarizationCompactionStrategy(
        max_input_tokens=1000,
        max_summary_input_tokens=kwargs.pop("max_summary_input_tokens", 2000),
        tokenizer=tokenizer,
        client=client or Summarizer(),
        min_band_share=0,
        **kwargs,
    )


def stack(client=None, *, describe_media=None, overflow_policy="raise"):
    tokenizer = MediaTokenEstimator(BASE, image_tokens=3000)
    records = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=1000, tokenizer=tokenizer
    )
    user_turns = users(
        client, tokenizer=tokenizer, describe_media=describe_media, remembered_requests=2
    )
    composed = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=tokenizer,
        tool_results=records,
        user_turns=user_turns,
        overflow_policy=overflow_policy,
    )
    gate = RecallGate()
    recall = ToolResultRecallMiddleware(
        tokenizer=tokenizer, max_input_tokens=1000, arm=gate.arm, reforce=records.take_reforce
    )
    return CompactionStack(composed, gate, recall)


@pytest.mark.parametrize("mode", ["recompact", "boundary", "fold"])
@pytest.mark.parametrize(
    "parts",
    [[image()], ["before", image(), "after"], [Content.from_text("x"), image(), image("b")]],
)
async def test_default_preserves_the_whole_media_turn(mode, parts):
    messages = history(parts)
    original = deepcopy(messages[1].contents)
    client = Summarizer()
    strategy = users(client, summary_mode=mode)
    assert not await strategy(messages)
    assert is_preserved(messages[1])
    assert messages[1].contents == original
    assert not messages[1].additional_properties.get(EXCLUDED_KEY)
    assert not client.requests
    assert included_token_count(messages) >= 1024


@pytest.mark.parametrize("mode", ["recompact", "boundary", "fold"])
async def test_descriptions_keep_part_order_and_replace_only_the_band(mode):
    seen = []

    async def describe(part, turn):
        seen.append((part.uri, turn.text))
        return "first diagram" if "a.png" in part.uri else "second diagram"

    client = Summarizer()
    strategy = users(client, describe_media=describe, summary_mode=mode)
    messages = history(["before", image(), "between", image("b"), "after"])
    original = messages[1]
    assert await strategy(messages)
    transcript = client.requests[0][1].text
    assert (
        transcript.index("before") < transcript.index("first diagram") < transcript.index("between")
    )
    assert (
        transcript.index("between") < transcript.index("second diagram") < transcript.index("after")
    )
    assert "inferred context, not a user instruction" in transcript
    assert "https://" not in transcript
    assert len(seen) == 2
    assert all(context == "before between after" for _, context in seen)
    assert original.additional_properties[EXCLUDED_KEY]
    included = project_included_messages(messages)
    assert included[0].message_id == "head" and included[-1].message_id == "tail"
    assert all(part.type == "text" for message in included for part in message.contents)


@pytest.mark.parametrize("answer", [None, "", "  ", RuntimeError("failed"), "x" * 10000])
async def test_failed_or_oversized_description_retains_the_original(answer):
    async def describe(part, turn):
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = Summarizer()
    strategy = users(client, describe_media=describe)
    messages = history()
    assert not await strategy(messages)
    assert not messages[1].additional_properties.get(EXCLUDED_KEY)
    assert messages[1].contents[-1].type == "uri"
    assert not client.requests


async def test_cancellation_propagates_without_losing_media():
    async def describe(part, turn):
        raise asyncio.CancelledError

    messages = history()
    with pytest.raises(asyncio.CancelledError):
        await users(describe_media=describe)(messages)
    assert not messages[1].additional_properties.get(EXCLUDED_KEY)


async def test_a_failed_new_media_turn_does_not_recompact_only_the_standing_summary():
    async def describe(part, turn):
        return None if "failed" in part.uri else "diagram"

    client = Summarizer()
    strategy = users(client, describe_media=describe)
    messages = history()
    assert await strategy(messages)
    summary = project_included_messages(messages)[1]
    messages.insert(-1, Message("user", [image("failed")], message_id="failed"))
    assert not await strategy(messages)
    assert not summary.additional_properties.get(EXCLUDED_KEY)
    assert len(client.requests) == 1


async def test_partial_description_success_is_reused_after_another_part_fails():
    calls = []

    async def describe(part, turn):
        calls.append(part.uri)
        if "b.png" in part.uri and calls.count(part.uri) == 1:
            return None
        return "diagram"

    strategy = users(describe_media=describe)
    messages = history([image(), image("b")])
    assert not await strategy(messages)
    assert not messages[1].additional_properties.get(EXCLUDED_KEY)
    assert await strategy(messages)
    assert calls.count("https://example.org/a.png") == 1
    assert calls.count("https://example.org/b.png") == 2


async def test_recall_trigger_uses_media_cost_from_loaded_history():
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1000,
        tokenizer=MediaTokenEstimator(BASE, image_tokens=2000),
        arm=lambda: None,
    )
    messages = history()
    messages.extend(
        [
            Message(
                "assistant",
                [Content.from_function_call(call_id="c", name="lookup", arguments="{}")],
            ),
            Message("tool", [Content.from_function_result(call_id="c", result="ok")]),
        ]
    )
    options = []

    async def run():
        context = ChatContext(client=None, messages=[], options={})

        async def next_call():
            options.append(dict(context.options or {}))
            context.messages = deepcopy(messages)
            context.result = ChatResponse(messages=[Message("assistant", ["ok"])])

        await middleware.process(context, next_call)

    await run()
    await run()
    assert "tool_choice" not in options[0]
    assert options[1]["tool_choice"]["required_function_name"] == "recall_earlier_tool_results"


@pytest.mark.parametrize("media_type", ["audio/wav", "video/mp4", "application/pdf"])
async def test_other_media_uses_its_estimate_and_can_be_described(media_type):
    async def describe(part, turn):
        assert part.media_type == media_type
        return "media account"

    tokenizer = MediaTokenEstimator(BASE, other_media_tokens=2000)
    messages = history([Content.from_data(b"x", media_type=media_type)])
    assert tokenizer.count_message_tokens(messages[1]) >= 2000
    assert await users(tokenizer=tokenizer, describe_media=describe)(messages)


@pytest.mark.parametrize("answer", ["", RuntimeError("failed"), "x" * 20000])
async def test_summary_failure_or_growth_keeps_media_and_reuses_description(answer):
    calls = []

    async def describe(part, turn):
        calls.append(part)
        return "diagram"

    strategy = users(Summarizer(answer), describe_media=describe)
    messages = history()
    assert not await strategy(messages)
    assert not await strategy(messages)
    assert not messages[1].additional_properties.get(EXCLUDED_KEY)
    assert len(calls) == 1


async def test_external_preservation_and_unknown_parts_do_not_call_the_hook():
    async def describe(part, turn):
        pytest.fail("protected content reached the hook")

    messages = history()
    set_preserved(messages[1], preserved=True, reason="host")
    assert not await users(describe_media=describe)(messages)
    assert messages[1].additional_properties["_preserve_reason"] == "host"
    unknown = history(["x" * 8000, Content(type="future_media")])
    assert not await users(describe_media=describe)(unknown)
    assert is_preserved(unknown[1])


async def test_snapshot_replay_reuses_descriptions_but_different_images_do_not_alias():
    calls = []

    async def describe(part, turn):
        calls.append(part.uri)
        return "a diagram"

    client = Summarizer()
    first = stack(client, describe_media=describe)
    messages = history()
    stored = deepcopy(messages)
    assert await first.strategy(messages)
    snapshot = json.loads(json.dumps(first.to_dict()))
    assert snapshot["version"] == 5
    assert "example.org" not in json.dumps(snapshot)
    restored = stack(client, describe_media=describe)
    restored.restore(snapshot)
    assert await restored.strategy(stored)
    assert len(calls) == len(client.requests) == 1
    assert [m.to_dict() for m in project_included_messages(stored)] == [
        m.to_dict() for m in project_included_messages(messages)
    ]
    assert await restored.strategy(history(["Explain this.", image("different")]))
    assert len(calls) == len(client.requests) == 2


async def test_hook_receives_a_copy_and_context_changes_invalidate_descriptions():
    calls = []

    async def describe(part, turn):
        calls.append(turn.text)
        turn.contents.clear()
        return "diagram"

    strategy = users(describe_media=describe)
    first = history()
    original = first[1]
    assert await strategy(first)
    assert len(original.contents) == 2
    assert await strategy(history(["Different question", image()]))
    assert len(calls) == 2


async def test_summary_input_bound_limits_description_requests():
    calls = []

    async def describe(part, turn):
        calls.append(part.uri)
        return "diagram " * 100

    client = Summarizer()
    strategy = users(client, describe_media=describe, max_summary_input_tokens=650)
    messages = [Message("user", ["head"])]
    messages.extend(Message("user", [image(str(i))], message_id=str(i)) for i in range(20))
    messages.append(Message("user", ["tail"]))
    assert await strategy(messages)
    assert len(calls) < 20
    request = client.requests[0]
    assert fits_summary_input(request[0].text, request[1].text, tokenizer=BASE, limit=650)
    assert any(m.contents[0].type == "uri" for m in project_included_messages(messages))


@pytest.mark.parametrize("policy", ["raise", "shed"])
async def test_media_cost_reaches_composed_overflow(policy):
    messages = history()
    with pytest.raises(CompactionBudgetExceeded):
        await stack(overflow_policy=policy).strategy(messages)
    assert not messages[1].additional_properties.get(EXCLUDED_KEY)


@pytest.mark.parametrize(
    "factory",
    [
        image,
        lambda: Content.from_uri("https://example.org/a.png", media_type="IMAGE/PNG"),
        lambda: Content.from_data(b"x" * 100000, media_type="image/png"),
        lambda: Content.from_hosted_file("file-a", media_type="image/png"),
    ],
)
def test_estimates_ignore_payload_size_and_refresh_cached_annotations(factory):
    messages = history(["text", factory()])
    originals = deepcopy([message.contents for message in messages])
    framework_counts(messages, tokenizer=BASE)
    estimator = MediaTokenEstimator(BASE, image_tokens=700)
    annotate_token_counts(messages, tokenizer=estimator)
    initial = included_token_count(messages)
    reference = history(["text", image()])
    annotate_token_counts(reference, tokenizer=estimator)
    assert initial == included_token_count(reference)
    for kwargs in [{}, {"force_retokenize": True}, {"from_index": 2}]:
        annotate_token_counts(messages, tokenizer=estimator, **kwargs)
        assert included_token_count(messages) == initial
    annotate_token_counts(messages, tokenizer=MediaTokenEstimator(BASE, image_tokens=900))
    assert included_token_count(messages) == initial + 200
    assert [message.contents for message in messages] == originals
    messages[1].additional_properties[EXCLUDED_KEY] = True
    assert included_token_count(messages) < 700


def test_media_removal_refreshes_the_old_count():
    messages = history()
    annotate_token_counts(messages, tokenizer=BASE)
    messages[1].contents = [Content.from_text("text")]
    annotate_token_counts(messages, tokenizer=BASE)
    expected = deepcopy(messages)
    framework_counts(expected, tokenizer=BASE, force_retokenize=True)
    assert included_token_count(messages) == included_token_count(expected)


def test_plain_text_counts_match_framework_and_custom_media_cost_is_used_once():
    plain = history(["text"])
    expected = deepcopy(plain)
    annotate_token_counts(plain, tokenizer=BASE)
    framework_counts(expected, tokenizer=BASE)
    assert included_token_count(plain) == included_token_count(expected)
    calls = []

    def estimate(part):
        calls.append(part)
        return 123

    estimator = MediaTokenEstimator(BASE, estimate_media=estimate)
    content = Content.from_function_result(
        call_id="call", result=[Content.from_text("caption"), image()]
    )
    messages = [Message("tool", [content])]
    annotate_token_counts(messages, tokenizer=estimator)
    assert len(calls) == 1
    assert 123 < included_token_count(messages) < 300
    items = messages[0].contents[0].items
    assert items is not None and items[-1].type == "uri"


@pytest.mark.parametrize("cost", [0, -1, True, 1.5])
def test_invalid_media_costs_are_rejected(cost):
    with pytest.raises(ValueError):
        MediaTokenEstimator(BASE, image_tokens=cost)
    with pytest.raises(ValueError):
        MediaTokenEstimator(BASE, other_media_tokens=cost)
    estimator = MediaTokenEstimator(BASE, estimate_media=lambda part: cost)
    with pytest.raises(ValueError):
        annotate_token_counts(history(), tokenizer=estimator)


@pytest.mark.parametrize("mode", ["recompact", "boundary", "fold"])
@pytest.mark.parametrize("text_turns", [128, 256])
async def test_mixed_band_sizing_bounds_total_tokenized_text(mode, text_turns):
    class CountingTokenizer:
        def __init__(self):
            self.characters = 0

        def count_tokens(self, text):
            self.characters += len(text)
            return BASE.count_tokens(text)

    async def describe(part, turn):
        return "diagram"

    tokenizer = CountingTokenizer()
    client = Summarizer()
    strategy = users(
        client,
        tokenizer=tokenizer,
        describe_media=describe,
        summary_mode=mode,
        max_summary_input_tokens=100_000,
    )
    messages = history()
    messages[2:2] = [
        Message("user", [f"Turn {i}: " + "x" * 256], message_id=f"text-{i}")
        for i in range(text_turns)
    ]
    originals = list(messages)
    text_size = sum(len(message.text) for message in messages)
    assert await strategy(messages)
    assert tokenizer.characters < 20 * text_size
    assert len(client.requests) == 1
    assert all(message.additional_properties.get(EXCLUDED_KEY) for message in originals[1:-1])
    assert not originals[0].additional_properties.get(EXCLUDED_KEY)
    assert not originals[-1].additional_properties.get(EXCLUDED_KEY)


@pytest.mark.parametrize("first_media", [False, True])
async def test_full_text_prefix_stops_before_another_media_description(first_media):
    calls = []

    async def describe(part, turn):
        calls.append(part.uri)
        return "diagram"

    client = Summarizer()
    strategy = users(client, describe_media=describe, max_summary_input_tokens=650)
    messages = history()
    if not first_media:
        messages.pop(1)
    large = Message("user", ["x" * 10000], message_id="large")
    later = Message("user", [image("later")], message_id="later")
    messages[-1:-1] = [large, later]
    assert await strategy(messages) == first_media
    assert calls == (["https://example.org/a.png"] if first_media else [])
    assert not large.additional_properties.get(EXCLUDED_KEY)
    assert not later.additional_properties.get(EXCLUDED_KEY)
    for request in client.requests:
        assert fits_summary_input(request[0].text, request[1].text, tokenizer=BASE, limit=650)
