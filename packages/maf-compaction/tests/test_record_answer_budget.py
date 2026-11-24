"""Tell the summarizer the output cap a record merge or rewrite request is sent with."""

from __future__ import annotations

from typing import Any

import pytest
from agent_framework import ChatResponse, Message
from test_record_budget import _CAP, _body, _history, _strategy, _Summarizer
from test_summary_prompt_boundary import record_attempt

from maf_compaction import _composed as composed
from maf_compaction._composed import (
    DEFAULT_RECORD_MERGE_PROMPT,
    RECORD_ANSWER_BUDGET_SHARE,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _numbered_records,
    harder_record_prompt,
    record_answer_budget,
)
from maf_compaction._summary_input import summary_input_tokens

pytestmark = pytest.mark.anyio


def test_the_sentence_states_the_cap_and_a_budget_below_it() -> None:
    sentence = record_answer_budget(8_192)
    assert "cut off after 8,192 tokens" in sentence
    assert f"within about {int(8_192 * RECORD_ANSWER_BUDGET_SHARE):,} tokens" in sentence
    assert "roughly 3,931 words" in sentence
    assert sentence.startswith(" "), "appended to a prompt that ends in a sentence"


def test_on_by_default() -> None:
    original = _strategy(_Summarizer(), budget=1000)
    strategy = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=original.tokenizer,
        tool_results=original.tool_results,
        user_turns=original.user_turns,
    )
    assert strategy.state_record_budget is True


@pytest.mark.parametrize(
    ("operation", "prompt"),
    [("merge", DEFAULT_RECORD_MERGE_PROMPT), ("rewrite", harder_record_prompt(1))],
)
async def test_a_capped_request_is_told_its_cap(operation: str, prompt: str) -> None:
    model = _Summarizer()
    model.rewrite = model.merge
    strategy = _strategy(model, budget=100_000)
    strategy.harder_attempts = 1

    assert await record_attempt(strategy, _history(2), operation)

    sent, _ = model.requests[0]
    assert sent == prompt + record_answer_budget(_CAP)


@pytest.mark.parametrize("why", ["no cap", "switched off", "options retired"])
async def test_a_request_without_a_cap_is_sent_as_before(why: str) -> None:
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000, cap=None if why == "no cap" else _CAP)
    if why == "switched off":
        strategy.state_record_budget = False
    if why == "options retired":
        strategy.user_turns._summary_options_disabled = True

    assert await record_attempt(strategy, _history(2), "merge")

    sent, _ = model.requests[0]
    assert sent == DEFAULT_RECORD_MERGE_PROMPT


async def test_the_sentence_does_not_change_which_batch_fits() -> None:
    """A pair whose plain request fills the request limit exactly is still merged: the batch is
    sized by the merge prompt alone, and the sentence is added to the request after."""
    messages = _history(2)
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000, share=0.001)
    strategy.record_request_input_tokens = summary_input_tokens(
        strategy.merge_prompt,
        _numbered_records([_body(0), _body(1)]),
        tokenizer=strategy.tokenizer,
    )

    assert await strategy._merge_batches(messages, None, 0.001)

    (sent, _) = model.requests[0]
    assert sent.endswith(record_answer_budget(_CAP))


def test_the_package_exports_it() -> None:
    assert "record_answer_budget" in composed.__all__
    assert "RECORD_ANSWER_BUDGET_SHARE" in composed.__all__


class _FailsWithOptions(_Summarizer):
    """Refuses the first request, as a provider refuses an option it does not take."""

    def __init__(self) -> None:
        super().__init__()
        self.options: list[dict[str, Any]] = []

    async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
        self.options.append(dict(kwargs.get("options") or {}))
        if len(self.options) == 1:
            self.requests.append((messages[0].text or "", messages[-1].text or ""))
            raise ValueError("Unsupported option")
        return await super().get_response(messages, **kwargs)


@pytest.mark.parametrize(
    ("operation", "prompt"),
    [("merge", DEFAULT_RECORD_MERGE_PROMPT), ("rewrite", harder_record_prompt(1))],
)
async def test_the_retry_without_options_is_not_told_a_cap_it_lacks(
    operation: str, prompt: str
) -> None:
    model = _FailsWithOptions()
    model.rewrite = model.merge
    strategy = _strategy(model, budget=100_000)
    strategy.harder_attempts = 1
    strategy.user_turns.retry_without_summary_options = True

    assert await record_attempt(strategy, _history(2), operation)

    assert model.options == [{"max_tokens": _CAP}, {}]
    (first, _), (retry, _) = model.requests
    assert first == prompt + record_answer_budget(_CAP)
    assert retry == prompt, "the uncapped retry is sent the plain prompt"


async def _plain_request_tokens() -> int:
    """What a plain merge request of two records counts, as the strategy measures it."""
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000)
    strategy.state_record_budget = False
    assert await record_attempt(strategy, _history(2), "merge")
    (prompt, transcript) = model.requests[0]
    return summary_input_tokens(prompt, transcript, tokenizer=strategy.tokenizer)


@pytest.mark.parametrize("room", [False, True])
async def test_the_sentence_never_takes_a_request_over_its_input_ceiling(room: bool) -> None:
    """At a ceiling the plain request fills exactly, the request goes out without the sentence
    rather than over the ceiling or not at all; with room for it, the sentence is sent."""
    plain = await _plain_request_tokens()
    model = _Summarizer()
    strategy = _strategy(model, budget=100_000)
    strategy.user_turns.max_summary_input_tokens = plain + (1_000 if room else 0)

    assert await record_attempt(strategy, _history(2), "merge")

    (sent, transcript) = model.requests[0]
    assert sent == DEFAULT_RECORD_MERGE_PROMPT + (record_answer_budget(_CAP) if room else "")
    assert (
        summary_input_tokens(sent, transcript, tokenizer=strategy.tokenizer)
        <= strategy.user_turns.max_summary_input_tokens
    )
