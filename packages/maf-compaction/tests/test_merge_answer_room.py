"""A record merge whose required values cannot fit the answer budget is not asked."""

from __future__ import annotations

import logging
import re

import pytest
from agent_framework import Message
from test_record_budget import _DETAIL, _record_pair, _sent_text, _strategy, _Summarizer
from test_record_merge_limits import _batch_size

from maf_compaction import SummaryRequestLimiter
from maf_compaction import _composed as composed_module
from maf_compaction._composed import ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy

pytestmark = pytest.mark.anyio

#: Ten tokens each under the character estimator, so a batch's required values are easy to price.
_VALUE_CHARS = 40
_VALUES_PER_RECORD = 10
_LONG_VALUE = re.compile(r"VAL-\d{3}-\d-x+")

#: Room for every batch these tests build, above the output cap, as the reported deployment had.
_REQUEST_TOKENS = 10_000
#: An answer budget of 200 tokens: a pair's 16 required values (160) fit, four records' 32 do not.
_CAP_FOR_PAIRS = 250
#: An answer budget of 120 tokens, below even a pair's 160.
_CAP_BELOW_A_PAIR = 150
_PARALLEL = 3
#: A token rate no request here comes near, so the limiter allows ``_PARALLEL`` at once.
_LIMITER_TOKENS = 10_000_000


def _values(index: int) -> list[str]:
    return [
        f"VAL-{index:03d}-{part}-".ljust(_VALUE_CHARS, "x") for part in range(_VALUES_PER_RECORD)
    ]


def _all_values(records: int) -> set[str]:
    return {value for index in range(records) for value in _values(index)}


def _history(records: int) -> list[Message]:
    messages = [
        Message(role="system", contents=["You are an assistant."], message_id="sys"),
        Message(role="user", contents=["Look everything up."], message_id="task"),
    ]
    for index in range(records):
        messages += _record_pair(index, body=" ".join(_values(index)) + ". " + _DETAIL)
    messages.append(Message(role="user", contents=["Now compare them."], message_id="last"))
    return messages


def _quote_values(body: str) -> str:
    return " ".join(sorted(set(_LONG_VALUE.findall(body))))


def _sent_values(messages: list[Message]) -> set[str]:
    return set(_LONG_VALUE.findall(_sent_text(messages)))


def _capped(
    model: _Summarizer, *, cap: int | None, share: float | None = 0.001
) -> ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy:
    strategy = _strategy(model, budget=100_000, share=share, cap=cap)
    strategy.record_request_input_tokens = _REQUEST_TOKENS
    return strategy


@pytest.mark.parametrize("share", [0.001, None])
async def test_a_batch_whose_required_values_cannot_fit_is_split_before_it_is_asked(
    share: float | None,
) -> None:
    """Four records fit one request, but the 32 values a merge must keep cost 320 tokens against
    an answer budget of 200: the batch is not asked, and its halves, at 160 each, are."""
    model = _Summarizer(merge=_quote_values)
    strategy = _capped(model, cap=_CAP_FOR_PAIRS, share=share)
    messages = _history(4)

    assert await strategy._merge_batches(messages, None, share)

    assert [_batch_size(body) for body in model.merges] == [2, 2]
    assert (strategy.record_merges_skipped, strategy.record_merges_rejected) == (1, 0)
    assert _sent_values(messages) == _all_values(4)


@pytest.mark.parametrize("why", ["no cap", "options retired"])
async def test_a_request_without_an_output_cap_is_asked_as_before(why: str) -> None:
    model = _Summarizer(merge=_quote_values)
    strategy = _capped(model, cap=None if why == "no cap" else _CAP_FOR_PAIRS)
    if why == "options retired":
        strategy.user_turns._summary_options_disabled = True
    messages = _history(4)

    assert await strategy._merge_batches(messages, None, 0.001)

    assert [_batch_size(body) for body in model.merges] == [4]
    assert strategy.record_merges_skipped == 0
    assert _sent_values(messages) == _all_values(4)


async def test_a_pair_without_answer_room_is_skipped_and_counted_until_coverage_admits_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pair cannot be split, so it is only skipped; the test is made again on every attempt,
    and a lower coverage share admits it."""
    model = _Summarizer(merge=_quote_values)
    strategy = _capped(model, cap=_CAP_BELOW_A_PAIR)
    messages = _history(2)

    with caplog.at_level(logging.INFO, logger=composed_module.logger.name):
        assert not await strategy._merge_batches(messages, None, 0.001)

    assert model.requests == []
    assert (strategy.record_merges_skipped, strategy.record_merges_rejected) == (1, 0)
    assert "values_total=20 required=16 required_tokens=160 answer_tokens=120" in caplog.text
    assert _sent_values(messages) == _all_values(2)

    strategy.tool_results.coverage_share = 0.5
    assert await strategy._merge_batches(messages, None, 0.001)
    assert [_batch_size(body) for body in model.merges] == [2]
    assert _sent_values(messages) == _all_values(2)


async def test_parallel_batches_without_answer_room_are_split_rather_than_asked() -> None:
    """Two batches of three, 240 required tokens each, would be asked side by side; each is
    split instead and only its fitting pair is asked."""
    model = _Summarizer(merge=_quote_values)
    strategy = _capped(model, cap=_CAP_FOR_PAIRS)
    strategy.record_merge_max_records = 3
    strategy.user_turns.summary_limiter = SummaryRequestLimiter(
        _LIMITER_TOKENS, max_parallel_summary_requests=_PARALLEL
    )
    messages = _history(6)
    assert strategy._summary_parallelism() == _PARALLEL, "the premise: batches are asked at once"

    assert await strategy._merge_batches(messages, None, 0.001)

    assert sorted(_batch_size(body) for body in model.merges) == [2, 2]
    assert strategy.record_merges_rejected == 0
    assert _sent_values(messages) == _all_values(6)
