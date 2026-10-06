"""Unit tests for the compaction / prompt-cache benchmark.

Everything here runs offline. The provider call is replaced by a stub so the replay loop,
the prefix oracle, and the aggregation can be verified without spending anything.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_framework import (
    CharacterEstimatorTokenizer,
    Content,
    Message,
    SlidingWindowStrategy,
    SummarizationStrategy,
)

from maf_cachebench import (
    DEFAULT_SYSTEM_TOKENS,
    CallOutcome,
    CellKey,
    ProviderCaller,
    ProviderRuntime,
    build_preset,
    build_provider,
    build_strategy,
    build_transcript,
    common_message_prefix,
    parse_provider_selector,
    percentile,
    prompt_cache_key,
    prompt_cache_key_options,
    render_summary_table,
    resolve_context_window,
    run_cell,
    serialize_message,
    strategy_names,
    summarize_cell,
    write_records_jsonl,
    write_summary_csv,
)
from maf_cachebench._live import find_nested_strategy
from maf_cachebench._runner import (
    is_connection_error,
    is_rate_limited,
    retry_after_seconds,
    unsupported_option,
)
from maf_cachebench._strategies import STRATEGIES_NEEDING_SUMMARIZER, StrategyOptions
from maf_cachebench._types import TurnRecord

pytestmark = pytest.mark.anyio

TOKENIZER = CharacterEstimatorTokenizer()


class StubCaller:
    """Records what it was asked to send and reports fixed usage."""

    def __init__(self, *, cached_tokens: int | None = None) -> None:
        self.calls: list[int] = []
        self.cached_tokens = cached_tokens

    async def __call__(self, messages: Sequence[Message]) -> CallOutcome:
        self.calls.append(len(messages))
        return CallOutcome(
            latency_ms=1.0,
            input_tokens=100,
            cached_tokens=self.cached_tokens,
            output_tokens=5,
        )


def test_unsupported_option_recognises_a_refused_tool_choice() -> None:
    """A refusal that names no parameter must still map to the option it refers to.

    Z.AI answers a pinned tool choice with "Tool choice must be auto", naming no field, so
    the generic patterns miss it and every turn fails.
    """
    error = Exception("Error code: 400 - {'error': {'message': 'Tool choice must be auto'}}")

    assert unsupported_option(error) == "tool_choice"


def _cell(strategy: str = "none") -> CellKey:
    return CellKey(
        provider="stub", model="stub-model", transcript="small", strategy=strategy, repeat=1
    )


# region transcripts


def test_transcript_is_deterministic_for_a_given_salt() -> None:
    first = build_transcript(name="t", turns=5, salt="abc")
    second = build_transcript(name="t", turns=5, salt="abc")
    assert serialize_message(first.system) == serialize_message(second.system)
    for left, right in zip(first.turns, second.turns):
        assert [serialize_message(m) for m in left.request] == [
            serialize_message(m) for m in right.request
        ]
        assert [serialize_message(m) for m in left.reply] == [
            serialize_message(m) for m in right.reply
        ]


def test_salt_isolates_the_cache_namespace() -> None:
    first = build_transcript(name="t", turns=2, salt="alpha")
    second = build_transcript(name="t", turns=2, salt="beta")
    left = first.system.contents[0].text or ""
    right = second.system.contents[0].text or ""
    # The salt must land at the very front, since provider caches match on exact prefixes.
    assert left != right
    assert left.startswith("[cachebench:")


def test_salt_does_not_change_the_transcript_size() -> None:
    # Salts of very different lengths must still yield identical token counts: the salt
    # feeds the auto context window, which sets the compaction budgets. A length-sensitive
    # salt makes a strategy retain different amounts for different providers.
    short = build_transcript(name="t", turns=6, salt="a")
    long = build_transcript(name="t", turns=6, salt="a-very-much-longer-cell-salt-string")
    assert len(short.system.contents[0].text or "") == len(long.system.contents[0].text or "")
    assert short.approx_final_prompt_tokens == long.approx_final_prompt_tokens


def test_transcript_emits_tool_call_groups() -> None:
    transcript = build_transcript(name="t", turns=6, salt="s", tool_call_every=3)
    tool_turns = [
        index
        for index, turn in enumerate(transcript.turns, start=1)
        if any(
            content.type == "function_call"
            for message in turn.reply
            for content in message.contents
        )
    ]
    assert tool_turns == [3, 6]


def test_transcript_messages_differ_between_positions() -> None:
    transcript = build_transcript(name="t", turns=8, salt="s", tool_call_every=0)
    bodies = {serialize_message(turn.request[0]) for turn in transcript.turns}
    # Identical message bodies would let unrelated prefixes match and inflate reuse.
    assert len(bodies) == 8


def test_system_anchor_clears_the_provider_cache_floor() -> None:
    transcript = build_transcript(name="t", turns=1, salt="s")
    assert TOKENIZER.count_tokens(transcript.system.contents[0].text or "") >= 1024


def test_build_preset_rejects_unknown_names() -> None:
    with pytest.raises(KeyError):
        build_preset("enormous", salt="s")


def test_build_transcript_rejects_zero_turns() -> None:
    with pytest.raises(ValueError, match="turns must be greater than 0"):
        build_transcript(name="t", turns=0, salt="s")


# region prefix oracle


def test_serialize_message_ignores_non_wire_fields() -> None:
    left = Message(role="user", contents=["hello"], message_id="id-one")
    right = Message(role="user", contents=["hello"], message_id="id-two")
    # Distinct message_ids must not make two otherwise identical messages look different,
    # because message_id is never sent to the provider. Counting it would report a broken
    # prefix on every turn and make every strategy look equally cache-hostile.
    assert left.message_id != right.message_id
    assert serialize_message(left) == serialize_message(right)


def test_serialize_message_distinguishes_roles_and_contents() -> None:
    assert serialize_message(Message(role="user", contents=["a"])) != serialize_message(
        Message(role="assistant", contents=["a"])
    )
    assert serialize_message(Message(role="user", contents=["a"])) != serialize_message(
        Message(role="user", contents=["b"])
    )


def test_serialize_message_covers_tool_content() -> None:
    call = Message(
        role="assistant",
        contents=[Content.from_function_call(call_id="c1", name="f", arguments="{}")],
    )
    other = Message(
        role="assistant",
        contents=[Content.from_function_call(call_id="c1", name="g", arguments="{}")],
    )
    assert serialize_message(call) != serialize_message(other)


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ([], ["a"], 0),
        (["a"], ["a", "b"], 1),
        (["a", "b"], ["a", "b", "c"], 2),
        (["a", "b"], ["a", "x", "c"], 1),
        (["a", "b", "c"], ["a", "b"], 2),
        (["x"], ["y"], 0),
    ],
)
def test_common_message_prefix(previous: list[str], current: list[str], expected: int) -> None:
    assert common_message_prefix(previous, current) == expected


@pytest.mark.parametrize(
    ("values", "fraction", "expected"),
    [
        ([], 0.5, None),
        ([4.0], 0.5, 4.0),
        ([1.0, 2.0, 3.0], 0.5, 2.0),
        ([3.0, 1.0, 2.0], 0.0, 1.0),
        ([1.0, 2.0, 3.0], 1.0, 3.0),
    ],
)
def test_percentile(values: list[float], fraction: float, expected: float | None) -> None:
    assert percentile(values, fraction) == expected


# region replay loop


async def test_uncompacted_replay_never_breaks_the_prefix() -> None:
    transcript = build_transcript(name="t", turns=6, salt="s")
    caller = StubCaller()
    records = await run_cell(
        cell=_cell("none"),
        transcript=transcript,
        strategy=None,
        tokenizer=TOKENIZER,
        caller=caller,
    )
    assert len(records) == 6
    # Without compaction each prompt is a pure extension of the last, so the whole
    # previous prompt stays cacheable.
    assert [record.prefix_broken for record in records] == [False] * 6
    assert records[0].reusable_prefix_tokens_local == 0
    assert all(record.reusable_prefix_tokens_local > 0 for record in records[1:])
    assert caller.calls == sorted(caller.calls), "prompt should grow monotonically"


async def test_compaction_breaks_the_prefix() -> None:
    transcript = build_transcript(name="t", turns=8, salt="s")
    records = await run_cell(
        cell=_cell("sliding_window"),
        transcript=transcript,
        strategy=SlidingWindowStrategy(keep_last_groups=2),
        tokenizer=TOKENIZER,
        caller=StubCaller(),
    )
    # This is the effect the whole benchmark exists to quantify: dropping older groups
    # invalidates the cached prefix.
    assert any(record.prefix_broken for record in records)
    assert max(record.history_messages for record in records) > max(
        record.sent_messages for record in records
    )


async def test_on_record_streams_results() -> None:
    seen: list[TurnRecord] = []
    await run_cell(
        cell=_cell(),
        transcript=build_transcript(name="t", turns=3, salt="s"),
        strategy=None,
        tokenizer=TOKENIZER,
        caller=StubCaller(),
        on_record=seen.append,
    )
    assert [record.turn for record in seen] == [1, 2, 3]


async def test_failed_turns_are_recorded_not_raised() -> None:
    class FailingCaller:
        async def __call__(self, messages: Sequence[Message]) -> CallOutcome:
            return CallOutcome(latency_ms=0.0, error="boom")

    records = await run_cell(
        cell=_cell(),
        transcript=build_transcript(name="t", turns=2, salt="s"),
        strategy=None,
        tokenizer=TOKENIZER,
        caller=FailingCaller(),
    )
    summary = summarize_cell(records, cell=_cell(), reports_cache_tokens=False)
    assert summary.errors == 2
    assert summary.total_input_tokens == 0


# region aggregation


def test_summary_ratios() -> None:
    records = [
        TurnRecord(
            cell=_cell(),
            turn=index,
            history_messages=10,
            sent_messages=8,
            sent_tokens_local=1000,
            reusable_prefix_tokens_local=800,
            prefix_broken=index == 2,
            input_tokens=1000,
            cached_tokens=400,
            output_tokens=10,
            latency_ms=float(index),
        )
        for index in (1, 2)
    ]
    summary = summarize_cell(records, cell=_cell(), reports_cache_tokens=True)
    assert summary.total_input_tokens == 2000
    assert summary.fresh_input_tokens == 1200
    assert summary.cache_hit_ratio == pytest.approx(0.4)
    assert summary.local_reusable_ratio == pytest.approx(0.8)
    # hit 0.4 over reuse 0.8 — a quotient of fractions, so the local estimator's
    # inflated token counts cancel instead of halving the result.
    assert summary.cache_realization == pytest.approx(0.5)
    assert summary.prefix_breaks == 1
    # 1200 fresh tokens at full price plus 800 cached tokens at a quarter price.
    assert summary.effective_input_tokens(0.25) == pytest.approx(1400)


def test_cached_tokens_are_clamped_to_the_input_they_are_part_of() -> None:
    records = [
        TurnRecord(
            cell=_cell(),
            turn=1,
            history_messages=4,
            sent_messages=4,
            sent_tokens_local=500,
            reusable_prefix_tokens_local=300,
            prefix_broken=False,
            input_tokens=1000,
            cached_tokens=4000,  # upstream inconsistency: more cached than prompt
            latency_ms=1.0,
        )
    ]
    summary = summarize_cell(records, cell=_cell(), reports_cache_tokens=True)
    assert summary.total_cached_tokens == 1000
    assert summary.cache_hit_ratio == pytest.approx(1.0)
    assert summary.fresh_input_tokens == 0


def test_hit_ratio_suppressed_when_a_turn_omits_the_input_count() -> None:
    records = [
        TurnRecord(
            cell=_cell(),
            turn=1,
            history_messages=4,
            sent_messages=4,
            sent_tokens_local=500,
            reusable_prefix_tokens_local=300,
            prefix_broken=False,
            input_tokens=None,  # provider dropped it on a cache hit
            cached_tokens=800,
            latency_ms=1.0,
        )
    ]
    summary = summarize_cell(records, cell=_cell(), reports_cache_tokens=True)
    assert summary.turns_missing_input == 1
    # Dividing 800 cached by a denominator the provider never sent would invent a number.
    assert summary.cache_hit_ratio is None


def test_missing_cache_reporting_is_not_a_zero_hit_rate() -> None:
    records = [
        TurnRecord(
            cell=_cell(),
            turn=1,
            history_messages=4,
            sent_messages=4,
            sent_tokens_local=500,
            reusable_prefix_tokens_local=300,
            prefix_broken=False,
            input_tokens=500,
            cached_tokens=None,
            latency_ms=2.0,
        )
    ]
    summary = summarize_cell(records, cell=_cell(), reports_cache_tokens=False)
    # A provider that reports nothing must not be shown as a 0% hit rate.
    assert summary.cache_hit_ratio is None
    assert summary.cache_realization is None
    assert summary.local_reusable_ratio == pytest.approx(0.6)


# region strategies


#: Strategies that call a model of their own and so cannot be built without a client.
#: Matched by name rather than listed, so a new summarizing variant is covered the day it
#: is registered instead of breaking this test.
SUMMARIZING = tuple(sorted(STRATEGIES_NEEDING_SUMMARIZER))


def test_every_registered_strategy_builds() -> None:
    options = StrategyOptions(
        tokenizer=TOKENIZER, max_context_window_tokens=8000, max_output_tokens=512
    )
    for name in strategy_names():
        if name in SUMMARIZING:
            continue
        strategy = build_strategy(name, options)
        assert (strategy is None) == (name == "none")


@pytest.mark.parametrize("name", SUMMARIZING)
def test_summarizing_strategies_require_a_client(name: str) -> None:
    options = StrategyOptions(
        tokenizer=TOKENIZER, max_context_window_tokens=8000, max_output_tokens=512
    )
    with pytest.raises(ValueError, match="summarizer client"):
        build_strategy(name, options)


@pytest.mark.parametrize("name", ["summarization", "token_budget_summarize"])
def test_the_summarizer_input_is_bounded_by_the_cells_input_budget(name: str) -> None:
    """Core's default bound skips every tool result larger than 8,000 tokens, for good.

    The framework's summarizer transcript carries each tool result in full and its group
    selection skips any single group over the bound, so under the default a cell whose tool
    results run to tens of thousands of tokens is never compacted by this row: the summarizer
    is called once a turn on the small groups and the prompt never leaves the window. The
    bound has to be the cell's own, which is the window less the output reservation.
    """
    options = StrategyOptions(
        tokenizer=TOKENIZER,
        max_context_window_tokens=128_000,
        max_output_tokens=2_048,
        summarizer=object(),
    )
    strategy = build_strategy(name, options)
    nested = find_nested_strategy(strategy, SummarizationStrategy)
    assert nested is not None
    assert nested.max_summary_input_tokens == 128_000 - 2_048


def test_unknown_strategy_rejected() -> None:
    options = StrategyOptions(
        tokenizer=TOKENIZER, max_context_window_tokens=8000, max_output_tokens=512
    )
    with pytest.raises(KeyError):
        build_strategy("does_not_exist", options)


def test_resolve_context_window_scales_to_the_transcript() -> None:
    # An auto window must sit below the transcript size or compaction never fires and the
    # benchmark measures nothing.
    assert resolve_context_window(40_000) < 40_000
    assert resolve_context_window(40_000) == 24_000
    assert resolve_context_window(40_000, override=9_000) == 9_000


def test_auto_window_floor_keeps_the_system_anchor_inside_the_eviction_budget() -> None:
    # ContextWindowCompactionStrategy evicts at half the input budget. If the anchor does
    # not fit under that half, compaction falls back to evicting the anchor itself and the
    # stable cacheable prefix disappears.
    window = resolve_context_window(100, max_output_tokens=512)
    eviction_budget = (window - 512) * 0.5
    assert eviction_budget > DEFAULT_SYSTEM_TOKENS


# region unsupported request options


def test_unsupported_option_is_extracted_from_a_wrapped_provider_error() -> None:
    # Verbatim from Foundry/gpt-5.6-luna. The framework wraps provider errors in an
    # exception whose args are a tuple, so str() repr's the payload and the quotes around
    # the parameter name arrive backslash-escaped — which silently defeated the first
    # version of this matcher and cost a full 120-call re-run.
    wrapped = Exception(
        "<class 'FoundryChatClient'> service failed to complete the prompt: Error code: 400 - "
        "{'error': {'message': \"Unsupported parameter: 'temperature' is not supported with "
        "this model.\", 'type': 'invalid_request_error', 'param': 'temperature'}}"
    )
    assert unsupported_option(wrapped) == "temperature"


def test_unsupported_option_ignores_unrelated_failures() -> None:
    assert unsupported_option(Exception("Error code: 429 - rate limit exceeded")) is None
    assert unsupported_option(Exception("Error code: 500 - internal")) is None


async def test_caller_drops_a_rejected_option_and_retries() -> None:
    class RejectsTemperature:
        def __init__(self) -> None:
            self.seen: list[dict[str, object]] = []

        async def get_response(self, messages: object, *, options: dict[str, object]) -> object:
            self.seen.append(dict(options))
            if "temperature" in options:
                raise ValueError(
                    "Unsupported parameter: 'temperature' is not supported with this model."
                )
            return type("R", (), {"usage_details": {"input_token_count": 7}, "text": "ok"})()

    client = RejectsTemperature()
    runtime = ProviderRuntime(
        client=client, model="m", options={"max_tokens": 16, "temperature": 0.0}
    )
    caller = ProviderCaller(runtime)
    outcome = await caller(())
    assert outcome.error is None
    assert outcome.input_tokens == 7
    assert "temperature" in client.seen[0] and "temperature" not in client.seen[1]
    # The retry must not mutate the shared runtime options other cells still use.
    assert "temperature" in runtime.options


# region rate limits


class _Throttled(Exception):
    """A 429 shaped the way an SDK raises one: a status on the error, headers on a response."""

    def __init__(self, headers: Mapping[str, str] | None = None) -> None:
        super().__init__("Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}")
        self.status_code = 429
        self.response = SimpleNamespace(headers=dict(headers or {}))


class _Refused(RuntimeError):
    """A provider SDK's own 429, with nothing in its text to say so."""

    status_code = 429


def test_a_429_is_found_through_the_wrapper_that_hides_it() -> None:
    """The class that knows it was a 429 is never the class that is caught.

    The framework wraps the provider SDK's error, and each provider raises a different one, so
    classifying by exception type would work for exactly one of them. The status on the cause
    is the signal that does not depend on wording, and the wrapper used here says nothing
    about rates -- so nothing but the chain walk can classify it.
    """
    wrapped = Exception("<class 'FoundryChatClient'> service failed to complete the prompt")
    wrapped.__cause__ = _Refused("refused")

    assert is_rate_limited(wrapped)


def test_a_429_is_found_in_the_text_when_the_wrapper_kept_no_object() -> None:
    """A wrapper that rendered its cause to a string still has to be classifiable.

    This is the shape the sweep actually failed on: ``ChatClientException`` carrying
    ``Error code: 429`` and ``rate_limit_exceeded`` as text and nothing else.
    """
    assert is_rate_limited(
        Exception("Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}")
    )
    assert is_rate_limited(Exception("HTTP 503: Too Many Requests"))


def test_a_status_number_inside_a_token_count_is_not_throttling() -> None:
    """A deterministic refusal must not be retried as though the limit would lift.

    A prompt-too-large error names the size that was refused, and 274,293 tokens contains the
    digits 429. Read as a status, a wall becomes a spike: six attempts and minutes of waiting
    before the same failure arrives anyway.
    """
    assert not is_rate_limited(
        Exception("Error code: 400 - supports at most 272000 tokens, got 274293")
    )
    assert not is_rate_limited(Exception("Error code: 500 - internal"))


def test_the_wait_a_provider_asks_for_is_read_off_its_response() -> None:
    """A named delay beats any schedule invented here, because it knows when the window refills."""
    assert retry_after_seconds(_Throttled({"Retry-After": "12"})) == 12.0
    # Case-insensitively: the header arrives capitalised from one provider and not another.
    assert retry_after_seconds(_Throttled({"retry-after": "12"})) == 12.0


def test_a_millisecond_retry_after_is_scaled_to_seconds() -> None:
    """``retry-after-ms`` is the same instruction in different units, not a different one."""
    assert retry_after_seconds(_Throttled({"retry-after-ms": "1500"})) == 1.5


def test_a_retry_after_that_is_not_a_count_leaves_the_schedule_to_decide() -> None:
    """An HTTP-date ``Retry-After`` is not parsed, so the exponential schedule answers instead.

    Misparsing a date is a wait of hours or of nothing; falling back is neither.
    """
    assert retry_after_seconds(_Throttled({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None
    assert retry_after_seconds(_Throttled()) is None


def _dropped() -> Exception:
    """Return a lost connection shaped the way one arrives: a wrapper over a transport error.

    ``APIConnectionError`` carries no status and says only "Connection error.", and the
    framework wraps that in turn, so what is caught is two removes from the object that knows
    the socket failed. The wrapper here says nothing at all, which is the point: only the cause
    can classify it.
    """
    wrapped = Exception("<class 'FoundryChatClient'> service failed to complete the prompt")
    wrapped.__cause__ = ConnectionResetError(104, "the peer went away")
    return wrapped


def test_a_lost_connection_is_found_through_the_wrapper_that_hides_it() -> None:
    """The class that knows the socket failed is never the class that is caught.

    Providers wrap differently, so a test on the exception's own type would work for exactly
    one of them, and a test on its name would break the first time a provider renamed one.
    Walking the chain to the transport error is what does not depend on either.
    """
    assert is_connection_error(_dropped())
    assert not is_rate_limited(_dropped())


def test_a_wrapper_with_nothing_under_it_is_not_a_lost_connection() -> None:
    """The cause is doing the work, and this is what says so.

    The wrapper above is deliberately silent, so the same wrapper with nothing beneath it must
    classify the other way. Without this, a detection that had quietly degraded into matching
    the wrapper's own wording would still pass every test above.
    """
    assert not is_connection_error(
        Exception("<class 'FoundryChatClient'> service failed to complete the prompt")
    )


def test_a_lost_connection_is_found_in_the_text_when_the_wrapper_kept_no_object() -> None:
    """A wrapper that rendered its cause to a string still has to be classifiable.

    ``APIConnectionError``'s own message is the whole of what survives that rendering, and it
    is "Connection error."
    """
    assert is_connection_error(Exception("Connection error."))
    assert is_connection_error(
        Exception("httpcore.RemoteProtocolError: server disconnected without response")
    )


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_a_provider_that_failed_to_serve_is_worth_re_sending(status: int) -> None:
    """A 5xx is the provider saying it failed, not that the request was wrong.

    From here that is the same event as the connection dropping: nothing came back that
    answers the question, and the same request may well be answered next time. It is the
    weaker half of this classification -- a deterministic 500 will be re-sent four times before
    failing -- which is why the attempt budget is small and the waits are seconds.
    """
    refused = Exception(f"Error code: {status}")
    refused.status_code = status  # type: ignore[attr-defined]

    assert is_connection_error(refused)


@pytest.mark.parametrize(
    "error",
    [
        Exception("Error code: 400 - supports at most 272000 tokens, got 274293"),
        Exception("Error code: 401 - Access denied due to invalid subscription key"),
        Exception("Unsupported parameter: 'timeout' is not supported with this model."),
    ],
    ids=["context_length", "auth", "an_option_that_happens_to_be_called_timeout"],
)
def test_a_refusal_the_provider_decided_on_is_not_a_lost_connection(error: Exception) -> None:
    """Re-sending a refusal spends the whole prompt again for the same answer.

    The last case is why every text marker is a phrase and not a word. A provider rejecting an
    option *called* ``timeout`` would match a bare "timeout", and the drop-and-retry path that
    refusal belongs to would never see it -- the same shape as reading the "429" inside a token
    count as a status.
    """
    assert not is_connection_error(error)


def test_a_rate_limit_and_a_lost_connection_are_not_each_other() -> None:
    """Two retries, two counters, and no call that lands in both.

    A 429 is a status the provider answered with, so it is a decision and not a failure to
    arrive; the transient statuses are enumerated so that it cannot be read as one by accident.
    Without this the same refusal could be charged to both counters and the table would say a
    seed had been throttled and reconnected for one event.
    """
    assert is_rate_limited(_Throttled()) and not is_connection_error(_Throttled())
    assert is_connection_error(_dropped()) and not is_rate_limited(_dropped())


# region provider selectors


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("openrouter", ("openrouter", None)),
        ("openrouter:openai/gpt-5.4-mini", ("openrouter", "openai/gpt-5.4-mini")),
        # Model ids legitimately contain colons, so only the first one separates.
        ("ollama:glm-5.2:cloud", ("ollama", "glm-5.2:cloud")),
        ("openrouter:z-ai/glm-5.2:free", ("openrouter", "z-ai/glm-5.2:free")),
        ("mistral:", ("mistral", None)),
    ],
)
def test_parse_provider_selector(selector: str, expected: tuple[str, str | None]) -> None:
    assert parse_provider_selector(selector) == expected


def test_the_azure_responses_route_is_the_foundry_request_path_on_a_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It must be the Responses client the foundry route delegates to, so two models' cells stay comparable."""
    from agent_framework_openai import OpenAIChatClient
    from agent_framework_openai._chat_client import RawOpenAIChatClient

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    runtime = build_provider(
        "azure-responses", temperature=None, response_max_tokens=64, model="gpt-6-luna"
    )

    assert isinstance(runtime.client, OpenAIChatClient)
    assert isinstance(runtime.client, RawOpenAIChatClient)
    assert runtime.model == "gpt-6-luna"
    assert parse_provider_selector("azure-responses:gpt-6-luna") == (
        "azure-responses",
        "gpt-6-luna",
    )
    assert prompt_cache_key_options("azure-responses", "salt-a", enable_optional=True) == {}, (
        "as on foundry"
    )


# region prompt cache key


def test_automatic_cache_providers_need_opting_in() -> None:
    # Measured: Mistral and Azure both cache without a key, so none of them send one by
    # default; an older deployment can reject the unknown field.
    for provider in ("mistral", "azure", "openrouter"):
        assert prompt_cache_key_options(provider, "salt-a") == {}


def test_mistral_takes_the_key_as_a_declared_option_not_extra_body() -> None:
    # MistralChatOptions declares prompt_cache_key, so it travels as a plain option;
    # OpenAI-SDK routes need it smuggled through extra_body instead.
    assert prompt_cache_key_options("mistral", "salt-a", enable_optional=True)[
        "prompt_cache_key"
    ].startswith("cachebench-")
    azure = prompt_cache_key_options("azure", "salt-a", enable_optional=True)
    assert azure["extra_body"]["prompt_cache_key"].startswith("cachebench-")


def test_providers_without_the_field_never_get_one() -> None:
    assert prompt_cache_key_options("ollama", "salt-a", enable_optional=True) == {}
    assert prompt_cache_key_options("foundry", "salt-a", enable_optional=True) == {}


def test_cache_key_is_stable_per_salt_and_distinct_across_cells() -> None:
    assert prompt_cache_key("salt-a") == prompt_cache_key("salt-a")
    assert prompt_cache_key("salt-a") != prompt_cache_key("salt-b")


def test_cell_options_do_not_clobber_provider_extra_body() -> None:
    runtime = ProviderRuntime(
        client=object(),
        model="m",
        options={"max_tokens": 16, "extra_body": {"provider": {"order": ["openai"]}}},
    )
    caller = ProviderCaller(runtime, extra_options={"extra_body": {"prompt_cache_key": "k"}})
    # Losing the routing pin would silently reintroduce the confound it exists to remove.
    assert caller.options["extra_body"] == {
        "provider": {"order": ["openai"]},
        "prompt_cache_key": "k",
    }
    assert caller.options["max_tokens"] == 16
    assert runtime.options["extra_body"] == {"provider": {"order": ["openai"]}}


# region reporting


def test_render_marks_unreported_cache_stats_as_not_available() -> None:
    records = [
        TurnRecord(
            cell=_cell(),
            turn=1,
            history_messages=2,
            sent_messages=2,
            sent_tokens_local=100,
            reusable_prefix_tokens_local=0,
            prefix_broken=False,
            input_tokens=100,
            latency_ms=1.0,
        )
    ]
    table = render_summary_table(
        [summarize_cell(records, cell=_cell(), reports_cache_tokens=False)]
    )
    assert "n/a" in table
    assert "hit%" in table


def test_render_empty() -> None:
    assert render_summary_table([]) == "No results."


def test_outputs_round_trip(tmp_path: Path) -> None:
    records = [
        TurnRecord(
            cell=_cell(),
            turn=1,
            history_messages=2,
            sent_messages=2,
            sent_tokens_local=100,
            reusable_prefix_tokens_local=50,
            prefix_broken=False,
            input_tokens=100,
            cached_tokens=50,
            latency_ms=1.0,
        )
    ]
    records_path = tmp_path / "nested" / "records.jsonl"
    assert write_records_jsonl(records_path, records) == 1
    payload = json.loads(records_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["provider"] == "stub"
    assert payload["cached_tokens"] == 50

    summary_path = tmp_path / "nested" / "summary.csv"
    summary = summarize_cell(records, cell=_cell(), reports_cache_tokens=True)
    assert write_summary_csv(summary_path, [summary], cache_read_ratio=0.25) == 1
    header = summary_path.read_text(encoding="utf-8").splitlines()[0]
    assert "cache_hit_ratio" in header
    assert "effective_input_tokens" in header
