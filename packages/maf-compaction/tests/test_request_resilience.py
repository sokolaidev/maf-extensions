"""Outgoing budgets and forced recall failure boundaries."""

import asyncio
import json
from copy import deepcopy

import pytest
from agent_framework import (
    CharacterEstimatorTokenizer,
    ChatContext,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionTool,
    Message,
    ResponseStream,
)
from agent_framework._compaction import EXCLUDED_KEY, included_token_count

from maf_compaction import (
    AnchoredCompactionStrategy,
    CompactionBudgetExceeded,
    RecallGate,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    UserTurnAnchoredSummarizationCompactionStrategy,
    make_recall_tool,
)
from maf_compaction._media import annotate_token_counts
from maf_compaction._request import RequestOverhead

pytestmark = pytest.mark.anyio
TOKENIZER = CharacterEstimatorTokenizer()


class Summarizer:
    async def get_response(self, *args, **kwargs):
        return ChatResponse(messages=Message("assistant", ["summary"]))


def composition(margin=0):
    return ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=TOKENIZER,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=1000, tokenizer=TOKENIZER
        ),
        user_turns=UserTurnAnchoredSummarizationCompactionStrategy(
            max_input_tokens=1000, tokenizer=TOKENIZER, client=Summarizer()
        ),
        estimate_margin=margin,
        overflow_policy="raise",
    )


async def test_call_overhead_and_margin_do_not_change_history_budget():
    strategy = composition(0.1)
    messages = [Message("user", ["x" * 3200])]
    assert not await strategy(deepcopy(messages))
    assert not await strategy.compact_before(deepcopy(messages), options={})
    with pytest.raises(CompactionBudgetExceeded) as failure:
        await strategy.compact_before(deepcopy(messages), options={"instructions": "x" * 800})
    assert failure.value.budget == 700
    assert strategy.max_input_tokens == 1000
    assert not await strategy(deepcopy(messages))
    assert not await strategy.compact_before(deepcopy(messages), options={})


@pytest.mark.parametrize("policy", ["raise", "shed"])
async def test_no_record_fallback_shortens_to_the_sending_ceiling(policy):
    ceilings = []

    class Fallback(AnchoredCompactionStrategy):
        async def compact_to(self, messages, *, ceiling):
            ceilings.append(ceiling)
            return await super().compact_to(messages, ceiling=ceiling)

    strategy = composition()
    strategy.overflow_policy = policy
    strategy.tool_results.fallback = Fallback(
        max_input_tokens=1000,
        tokenizer=TOKENIZER,
        keep_head_groups=1,
        keep_tail_groups=1,
        keep_tokens=100,
    )
    messages = [
        Message("user", ["head"]),
        Message(
            "assistant", [Content.from_function_call(call_id="call", name="lookup", arguments={})]
        ),
        Message("tool", [Content.from_function_result(call_id="call", result="x" * 2400)]),
        Message("user", ["tail"]),
    ]
    annotate_token_counts(messages, tokenizer=TOKENIZER)
    assert 600 < included_token_count(messages) < 800
    assert not await strategy(deepcopy(messages))
    original = messages[2]
    assert await strategy.compact_before(messages, options={"instructions": "x" * 1600})
    assert ceilings == [600]
    assert included_token_count(messages) <= 600
    assert not original.additional_properties.get(EXCLUDED_KEY)
    assert len(original.contents[0].result) < 2400
    assert strategy.max_input_tokens == strategy.tool_results.max_input_tokens == 1000


async def test_middleware_applies_live_options_to_composed_callback():
    strategy = composition()
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1000, tokenizer=TOKENIZER, arm=gate.arm
    )
    context = ChatContext(None, [Message("user", ["x" * 3200])], {"instructions": "y" * 1200})
    context.kwargs["compaction_strategy"] = strategy
    contacted = False

    async def send():
        nonlocal contacted
        await context.kwargs["compaction_strategy"](context.messages)
        contacted = True

    with pytest.raises(CompactionBudgetExceeded):
        await middleware.process(context, send)
    assert not contacted


def test_schema_cache_tracks_mutations_on_the_same_tool():
    overhead = RequestOverhead(TOKENIZER)
    tool = FunctionTool(name="recall_earlier_tool_results", func=make_recall_tool())
    first = overhead.count({"tools": [tool]})
    assert overhead.count({"tools": [tool]}) == first
    tool.description += " additional details" * 50
    assert overhead.count({"tools": [tool]}) > first
    spec = tool.to_json_schema_spec()
    baseline = overhead.count({"tools": spec})
    spec["function"]["parameters"]["properties"]["values"]["description"] = "extra" * 200
    assert overhead.count({"tools": spec}) > baseline
    assert overhead.count({"tools": []}) == 0


@pytest.mark.parametrize("margin", [-1, 1, float("nan"), float("inf"), True])
def test_invalid_estimate_margin(margin):
    with pytest.raises(ValueError, match="estimate_margin"):
        composition(margin)


def recall(**kwargs):
    gate = RecallGate()
    middleware = ToolResultRecallMiddleware(
        max_input_tokens=1000, tokenizer=TOKENIZER, arm=gate.arm, disarm=gate.disarm, **kwargs
    )
    middleware._force_next = True
    middleware._reforce_next = True
    return middleware, gate


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"tools": []},
        {"tools": None},
        {"tools": [make_recall_tool()], "tool_choice": "none"},
        {"tools": [make_recall_tool()], "tool_choice": {"mode": "none"}},
    ],
)
async def test_tools_disabled_leave_the_ask_pending_without_ignored_pin(options):
    middleware, gate = recall()
    middleware._ignored_streak = 1
    context = ChatContext(None, [], options)

    async def answer():
        assert context.options is options
        assert not gate.take()
        context.result = ChatResponse(messages=Message("assistant", ["final answer"]))

    await middleware.process(context, answer)
    state = middleware.session_state()
    assert state["force_next"] and state["reforce_next"]
    assert state["ignored_streak"] == 1
    assert middleware.forced_calls == middleware.ignored_forced_calls == 0
    context.options = {"tools": [make_recall_tool()]}

    async def record():
        assert context.options is not None
        choice = context.options["tool_choice"]
        assert isinstance(choice, dict)
        assert choice["required_function_name"] == "recall_earlier_tool_results"
        context.result = ChatResponse(
            messages=Message(
                "assistant",
                [
                    Content.from_function_call(
                        call_id="record",
                        name="recall_earlier_tool_results",
                        arguments={"values": "SKU-123"},
                    )
                ],
            )
        )

    await middleware.process(context, record)
    assert middleware.forced_calls == 1
    assert middleware._ignored_streak == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "arguments",
    ["[1]", '"text"', "null", "broken", '{"raw":"<tool_calls>"}', "{}", '{"values": 1}'],
)
async def test_malformed_recall_is_discarded_before_one_unpinned_retry(stream, arguments):
    middleware, gate = recall()
    options = {"tools": [make_recall_tool()], "temperature": 0.3}
    context = ChatContext(None, [Message("user", ["question"])], options, stream=stream)
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        if calls == 1:
            contents = [
                Content.from_function_call(
                    call_id="bad", name="recall_earlier_tool_results", arguments=arguments
                )
            ]
        else:
            assert context.options is options
            assert context.result is None
            assert not gate.take()
            contents = [Content.from_text("answer")]
        response = ChatResponse(messages=Message("assistant", contents))
        if stream:

            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=contents)

            context.result = ResponseStream(updates(), finalizer=lambda _: response)
        else:
            context.result = response

    await middleware.process(context, send)
    if stream:
        assert isinstance(context.result, ResponseStream)
        result = await context.result.get_final_response()
    else:
        assert isinstance(context.result, ChatResponse)
        result = context.result
    assert result.text == "answer"
    assert calls == 2
    assert middleware.failed_forced_calls == 1
    assert middleware.pin_disabled
    assert "bad" not in json.dumps([message.to_dict() for message in context.messages])


@pytest.mark.parametrize("stream", [False, True])
async def test_forced_deadline_cancels_before_retry(stream):
    middleware, gate = recall(forced_call_timeout_seconds=0.01)
    context = ChatContext(None, [], {"tools": [make_recall_tool()]}, stream=stream)
    calls = 0
    cancelled = asyncio.Event()

    async def hang():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def send():
        nonlocal calls
        calls += 1
        if calls == 1:
            if stream:

                async def updates():
                    yield ChatResponseUpdate(
                        role="assistant", contents=[Content.from_text("hidden")]
                    )
                    await hang()

                context.result = ResponseStream(
                    updates(), finalizer=lambda _: ChatResponse(messages=[])
                )
            else:
                await hang()
        else:
            assert cancelled.is_set()
            assert not gate.take()
            context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await middleware.process(context, send)
    assert calls == 2
    assert isinstance(context.result, ChatResponse)
    assert context.result.text == "answer"
    assert middleware.failed_forced_calls == 1


@pytest.mark.parametrize("seconds", [0, -1, True, float("inf"), float("nan")])
def test_invalid_forced_deadline(seconds):
    with pytest.raises(ValueError, match="forced_call_timeout_seconds"):
        recall(forced_call_timeout_seconds=seconds)
