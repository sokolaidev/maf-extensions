"""The records' own budget: a lossless reshape, bounded batch merges, and the shed as a backstop.

A long tool-heavy conversation keeps every record, and each record the model wrote is paid twice,
once in the recall call's arguments and again in its result. These tests drive the composed row
over histories made almost entirely of such records: the reshape pays each once with no
summarizer, the batches keep every merge request within the summarizer's output cap, a refused
batch stands until it changes, and the overflow shed is no longer what the prompt depends on.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections import Counter
from typing import Any, Literal

import pytest
from agent_framework import Annotation, CharacterEstimatorTokenizer, ChatResponse, Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
    project_included_messages,
)

from maf_compaction import _composed as composed_module
from maf_compaction._composed import (
    _MAX_BATCH_PROBES,
    DEFAULT_RECORD_BUDGET_SHARE,
    DEFAULT_RECORD_MERGE_PROMPT,
    CompactionBudgetExceeded,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _has_prefix,
)
from maf_compaction._preserve import STANDS_IN_KEY
from maf_compaction._summary_input import fits_summary_input
from maf_compaction._toolsummary import (
    RECALL_TOOL_NAME,
    ToolResultAnchoredSummarizationCompactionStrategy,
    _is_written_record,
    _newest_record_identity,
    _Reforce,
    active_record_groups,
    build_record_message,
    consolidatable_record_groups,
    make_recall_tool,
    reshapeable_record_groups,
)
from maf_compaction._usersummary import (
    SUMMARY_MODE_BOUNDARY,
    UserTurnAnchoredSummarizationCompactionStrategy,
)

pytestmark = pytest.mark.anyio

TOKENIZER = CharacterEstimatorTokenizer()

#: Filler in each record's body, so the copy in the call's arguments is worth paying once.
_DETAIL = "The lookup succeeded and its findings are listed above. " * 12

#: The summarizer's output cap the batch tests configure: about three records' worth.
_CAP = 1_200

_VALUE = re.compile(r"VAL-\d{3}-\d")


def _values(index: int) -> list[str]:
    return [f"VAL-{index:03d}-{part}" for part in range(4)]


def _body(index: int) -> str:
    return f"lookup_{index} returned " + " ".join(_values(index)) + ". " + _DETAIL


def _record_pair(index: int, *, body: str | None = None) -> list[Message]:
    """Return a record as the model writes one: the values in the call and again in its result."""
    text = _body(index) if body is None else body
    return [
        Message(
            role="assistant",
            contents=[
                {
                    "type": "function_call",
                    "call_id": f"rec{index}",
                    "name": RECALL_TOOL_NAME,
                    "arguments": json.dumps({"values": text}),
                }
            ],
            message_id=f"rec{index}_call",
        ),
        Message(
            role="tool",
            contents=[
                {
                    "type": "function_result",
                    "call_id": f"rec{index}",
                    "result": make_recall_tool()(text),
                }
            ],
            message_id=f"rec{index}_res",
        ),
    ]


def _tool_group(index: int, chars: int = 400) -> list[Message]:
    """Return an ordinary tool call and its result, which no record covers."""
    return [
        Message(
            role="assistant",
            contents=[
                {
                    "type": "function_call",
                    "call_id": f"call{index}",
                    "name": f"lookup_{index}",
                    "arguments": "{}",
                }
            ],
            message_id=f"c{index}",
        ),
        Message(
            role="tool",
            contents=[
                {
                    "type": "function_result",
                    "call_id": f"call{index}",
                    "result": f"code=CODE-{index:04d} " + "x" * chars,
                }
            ],
            message_id=f"r{index}",
        ),
    ]


def _history(records: int) -> list[Message]:
    """Return a conversation that is nothing but its task, its records and a closing turn."""
    messages = [
        Message(role="system", contents=["You are an assistant."], message_id="sys"),
        Message(role="user", contents=["Look everything up."], message_id="task"),
    ]
    for index in range(records):
        messages += _record_pair(index)
    messages.append(Message(role="user", contents=["Now compare them."], message_id="last"))
    return messages


def _size(messages: list[Message]) -> int:
    annotate_message_groups(messages)
    annotate_token_counts(messages, tokenizer=TOKENIZER, force_retokenize=True)
    return included_token_count(messages)


def _record_size(messages: list[Message]) -> int:
    _size(messages)
    return included_token_count(
        [
            message
            for group in active_record_groups(messages)
            for message in messages[group["start_index"] : group["end_index"] + 1]
        ]
    )


def _sent_text(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in project_included_messages(messages):
        for content in message.contents:
            parts.append(str(content.result) if content.type == "function_result" else str(content))
            parts.append(content.text or "" if content.type == "text" else "")
    return "\n".join(parts)


def _sent(messages: list[Message]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Return what the model is sent: each included message's role and contents."""
    return [
        (str(message.role), [content.to_dict() for content in message.contents])
        for message in project_included_messages(messages)
    ]


def _sent_values(messages: list[Message]) -> set[str]:
    return set(_VALUE.findall(_sent_text(messages)))


def _included_record_calls(messages: list[Message]) -> list[str]:
    return [
        str(content.call_id)
        for message in project_included_messages(messages)
        for content in message.contents
        if content.type == "function_call" and content.name == RECALL_TOOL_NAME
    ]


class _Summarizer:
    """A summarizer with configurable merge and rewrite answers."""

    def __init__(self, merge: Any = None, rewrite: Any = None) -> None:
        self.merge = merge or (lambda body: " ".join(sorted(set(_VALUE.findall(body)))))
        self.rewrite = rewrite or (lambda body: "Short summary.")
        self.requests: list[tuple[str, str]] = []

    @property
    def merges(self) -> list[str]:
        return [body for prompt, body in self.requests if prompt == DEFAULT_RECORD_MERGE_PROMPT]

    async def get_response(
        self, messages: list[Message], *, stream: bool = False, **kwargs: Any
    ) -> ChatResponse:
        prompt, body = messages[0].text or "", messages[-1].text or ""
        self.requests.append((prompt, body))
        text = self.merge(body) if prompt == DEFAULT_RECORD_MERGE_PROMPT else self.rewrite(body)
        return ChatResponse(messages=[Message(role="assistant", contents=[text])])


def _strategy(
    summarizer: _Summarizer,
    *,
    budget: int,
    share: float | None = DEFAULT_RECORD_BUDGET_SHARE,
    cap: int | None = _CAP,
    policy: Literal["shed", "raise"] = "raise",
    tokenizer: CharacterEstimatorTokenizer = TOKENIZER,
) -> ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy:
    """Return the composed row with a tiny head and tail, and its record half never waiting."""
    return ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=tokenizer,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=budget,
            tokenizer=tokenizer,
            keep_head_groups=1,
            keep_tail_groups=1,
            trigger_fraction=0.01,
            fallback_fraction=0.99,
        ),
        user_turns=UserTurnAnchoredSummarizationCompactionStrategy(
            max_input_tokens=budget,
            tokenizer=tokenizer,
            client=summarizer,
            trigger_fraction=0.01,
            summary_mode=SUMMARY_MODE_BOUNDARY,
            remembered_requests=2,
            summary_options=None if cap is None else {"max_tokens": cap},
        ),
        overflow_policy=policy,
        harder_attempts=0,
        record_budget_share=share,
    )


@pytest.mark.parametrize("share", [None, 1.0])
async def test_records_alone_over_the_budget_fit_by_the_reshape_with_no_summarizer(
    share: float | None,
) -> None:
    """The model's records exceed the whole budget, and paying each once brings them under it."""
    messages = _history(12)
    pairs = _record_size(messages)
    budget = pairs - 200
    assert pairs > budget, "the premise: the records alone exceed the budget"
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=budget, share=share)
    results = [
        str(content.result)
        for message in messages
        for content in message.contents
        if content.type == "function_result"
    ]

    await strategy(messages)

    assert summarizer.requests == [], "no summarizer call"
    assert _size(messages) <= budget
    assert strategy.records_reshaped == 12
    written = [m.text for m in project_included_messages(messages) if _is_written_record(m)]
    assert written == results, "each record's text exactly as the recall tool returned it"
    assert _sent_values(messages) == {value for index in range(12) for value in _values(index)}
    assert _included_record_calls(messages) == [], "no record is paid in a call's arguments"
    assert _record_size(messages) * 10 < pairs * 6, "about half: the second copy is gone"


async def test_records_over_their_share_are_merged_in_batches_within_the_output_cap() -> None:
    """After the reshape the records still exceed their share; bounded batches bring them under."""
    messages = _history(16)
    budget = _record_size(messages) - 200
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=budget, share=0.25)
    every_value = {value for index in range(16) for value in _values(index)}

    await strategy(messages)

    assert _record_size(messages) <= budget // 4, "the records fit their share"
    assert len(summarizer.merges) >= 2, "in more than one batch"
    for body in summarizer.merges:
        assert fits_summary_input(
            DEFAULT_RECORD_MERGE_PROMPT, body, tokenizer=TOKENIZER, limit=_CAP
        )
        assert body.count("lookup_") < 16, "never every record at once"
    assert strategy.records_merged == len(summarizer.merges)
    assert _sent_values(messages) == every_value, "a merge that keeps values loses none"
    assert _size(messages) <= budget


async def test_a_lossy_batch_is_refused_unchanged_and_not_asked_again_until_it_changes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A batch whose answer drops values stands as it was, and its merge is not asked again."""
    messages = _history(16)
    budget = _record_size(messages) - 200
    summarizer = _Summarizer(merge=lambda body: _VALUE.findall(body)[0])
    strategy = _strategy(summarizer, budget=budget)
    every_value = {value for index in range(16) for value in _values(index)}

    with caplog.at_level(logging.WARNING, logger="maf_compaction._composed"):
        await strategy(messages)

    asked = list(summarizer.merges)
    assert asked, "the premise: batches were asked for"
    assert strategy.record_merges_rejected == strategy.record_consolidations_lossy == len(asked)
    assert strategy.records_merged == 0
    assert _sent_values(messages) == every_value, "every record still being sent"
    assert len(active_record_groups(messages)) == 16
    refusals = [r.getMessage() for r in caplog.records if "batch of" in r.getMessage()]
    assert len(refusals) == len(asked)
    assert all("dropping values" in line for line in refusals)
    assert not _VALUE.search(caplog.text), "counts, never values"

    # Over the budget again on a narrower call: the same batches are not asked for again.
    options = {"instructions": "a" * 4 * (budget - _size(messages) + 100)}
    assert strategy.sending_budget(options) < _size(messages), "the premise: over on this call"
    with pytest.raises(CompactionBudgetExceeded):
        await strategy.compact_before(messages, options=options)
    assert summarizer.merges == asked
    assert strategy.record_merges_skipped >= len(asked)

    # A record that changes changes its batch, and only that batch is asked for again.
    (result,) = (m for m in messages if m.message_id == "rec0_res")
    stand_in = next(
        m
        for m in messages
        if _is_written_record(m) and not m.additional_properties.get(EXCLUDED_KEY)
    )
    assert "VAL-000-3" in (stand_in.text or "") and result.additional_properties[EXCLUDED_KEY]
    stand_in.contents = [Content.from_text((stand_in.text or "").replace("VAL-000-3", "VAL-000-9"))]
    with pytest.raises(CompactionBudgetExceeded):
        await strategy.compact_before(messages, options=options)
    assert len(summarizer.merges) == len(asked) + 1
    assert "VAL-000-9" in summarizer.merges[-1]


async def test_the_measured_shape_fits_without_the_overflow_shed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Many records over the budget, a few unrecorded groups and user turns, tiny head and tail.

    The shape a long tool-heavy run reaches: records alone over the sending budget, the
    unrecorded tool work all in the tail, plain user turns between records. Before the records
    had a budget of their own, every pass here shed the newest unrecorded group and still raised.
    """
    messages = [
        Message(role="system", contents=["You are an assistant."], message_id="sys"),
        Message(role="user", contents=["Audit every deployment."], message_id="task"),
    ]
    for index in range(30):
        messages += _record_pair(index)
        if index % 3 == 2:
            messages.append(
                Message(role="user", contents=[f"Next batch {index}."], message_id=f"u{index}")
            )
    for index in range(3):
        messages += _tool_group(100 + index)
    unrecorded = {f"c{100 + index}" for index in range(3)} | {
        f"r{100 + index}" for index in range(3)
    }
    budget = int(_record_size(messages) / 1.04)
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=budget, policy="shed")
    every_value = {value for index in range(30) for value in _values(index)}

    with caplog.at_level(logging.WARNING):
        await strategy(messages)

    assert "lost message IDs" not in caplog.text, "nothing shed"
    assert _size(messages) <= budget
    assert all(
        not m.additional_properties.get(EXCLUDED_KEY)
        for m in messages
        if m.message_id in unrecorded
    ), "the unrecorded tool work is all still there"
    assert _sent_values(messages) == every_value
    assert _record_size(messages) <= budget // 2


async def test_a_reshape_keeps_the_records_identity_and_replays_without_duplicating() -> None:
    """The reshaped record is the record it was, on every list a later pass is handed."""
    original = _history(12)
    budget = _record_size(copy.deepcopy(original)) - 200
    summarizer = _Summarizer()
    first = _strategy(summarizer, budget=budget, share=None)
    stored = copy.deepcopy(original)
    annotate_message_groups(stored)
    identity = _newest_record_identity(stored)
    assert identity.startswith("message:rec11_call"), "the premise: named by its call"

    await first(stored)

    assert first.records_reshaped == 12
    assert _newest_record_identity(stored) == identity, "the newest record keeps its name"
    stand_ins = [m for m in stored if _is_written_record(m)]
    assert len(stand_ins) == 12
    assert {m.additional_properties[STANDS_IN_KEY] for m in stand_ins} == {
        f"message:rec{index}_call:0" for index in range(12)
    }
    sent = _sent(stored)

    # The stored list itself, on the next pass: recognised as records, nothing redone.
    assert first.records_in_conversation == 12
    await first(stored)
    assert _sent(stored) == sent
    assert len([m for m in stored if _is_written_record(m)]) == 12
    assert first.records_reshaped == 12

    # A host that rebuilds the original history each run, and restores the session's state.
    cold = _strategy(summarizer, budget=budget, share=None)
    cold.restore_session_state(json.loads(json.dumps(first.session_state())))
    rebuilt = copy.deepcopy(original)
    await cold(rebuilt)
    assert _sent(rebuilt) == sent
    assert cold.records_reshaped == 0, "replayed, not decided again"
    assert cold.chain_decisions_kept == 1
    assert _newest_record_identity(rebuilt) == identity
    assert cold.tool_results.last_record_value_judgment == (
        first.tool_results.last_record_value_judgment
    ), "the record phase reads a replayed stand-in as the record it was"

    # A store that kept the written records but lost the exclusions: no record is written twice,
    # and the record phase never counts a record in both forms, which would read as a new one.
    flagless = copy.deepcopy(stored)
    for message in flagless:
        message.additional_properties.pop(EXCLUDED_KEY, None)
    cold.tool_results._reforce = _Reforce(targets=frozenset({"pending"}), records=12)
    await cold(flagless)
    assert len([m for m in flagless if _is_written_record(m)]) == 12
    assert _sent(flagless) == sent
    assert cold.records_in_conversation == 12
    pending = cold.tool_results.decision_state().reforce
    assert pending is not None and pending.passes == 1, "the pending recall still waits"
    assert summarizer.requests == []


async def test_a_reshape_keeps_the_payload_byte_for_byte_whitespace_included() -> None:
    """Indentation and trailing whitespace the model wrote survive the reshape and its replay."""
    values = "  lookup_50:\n    - VAL-050-0  \n\t- VAL-050-1\t\n\n"
    messages = _history(6)
    messages[-1:-1] = _record_pair(50, body=values)
    original = copy.deepcopy(messages)
    budget = _record_size(copy.deepcopy(messages)) - 200
    first = _strategy(_Summarizer(), budget=budget, share=None)

    await first(messages)

    (stand_in,) = (
        m for m in messages if m.additional_properties.get(STANDS_IN_KEY) == "message:rec50_call:0"
    )
    assert stand_in.text == make_recall_tool()(values), "the recall result, byte for byte"
    cold = _strategy(_Summarizer(), budget=budget, share=None)
    cold.restore_session_state(json.loads(json.dumps(first.session_state())))
    rebuilt = copy.deepcopy(original)
    await cold(rebuilt)
    assert cold.records_reshaped == 0, "replayed under the same exact payload"
    assert _sent(rebuilt) == _sent(messages)


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    ("operation", "written"),
    [("reshape", False), ("merge", False), ("merge", True), ("rewrite", False), ("rewrite", True)],
)
async def test_record_replacements_survive_calls_sharing_original_messages(
    operation: str, written: bool, restore: bool
) -> None:
    records = 1 if operation == "rewrite" else 9
    carried = _history(records)
    if written:
        carried[2:-1] = [build_record_message(_body(index)) for index in range(records)]
    originals = list(carried[2:-1])
    budget = _record_size(carried) - 200 if operation == "reshape" else 350
    if operation == "merge":
        budget = 1100
    summarizer = _Summarizer(rewrite=lambda body: " ".join(sorted(set(_VALUE.findall(body)))))
    strategy = _strategy(summarizer, budget=budget, share=None, cap=None)
    strategy.harder_attempts = 1
    strategy.user_turns.max_summary_input_tokens = 100_000
    outgoing = list(carried)

    await strategy(outgoing)

    if operation == "reshape":
        assert strategy.records_reshaped == records
        assert strategy.records_merged == strategy.record_rewrites == 0
    elif operation == "merge":
        assert strategy.records_merged == 1
    else:
        assert strategy.record_rewrites == 1
    expected = _sent_values(outgoing)
    assert expected == {value for index in range(records) for value in _values(index)}
    expected_records = [
        m.text for m in project_included_messages(outgoing) if _is_written_record(m)
    ]
    asked = len(summarizer.requests)

    strategy.max_input_tokens = 100_000
    if restore:
        cold = _strategy(summarizer, budget=100_000, share=None, cap=None)
        cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
        strategy = cold

    for index in range(3):
        carried.extend(_tool_group(100 + index))
        outgoing = list(carried)
        await strategy(outgoing)
        assert _sent_values(outgoing) == expected
        assert [
            m.text for m in project_included_messages(outgoing) if _is_written_record(m)
        ] == expected_records
        assert not _included_record_calls(outgoing)
        assert len(summarizer.requests) == asked

    assert all(not m.additional_properties.get(EXCLUDED_KEY) for m in originals)

    await strategy(outgoing)
    assert [
        m.text for m in project_included_messages(outgoing) if _is_written_record(m)
    ] == expected_records
    assert len(summarizer.requests) == asked


@pytest.mark.parametrize("shape", ["preceding", "co-located"])
async def test_a_record_with_a_reasoning_item_is_left_whole_and_no_reasoning_is_orphaned(
    shape: str,
) -> None:
    """A reasoning item stays with the call it precedes, as every exclusion here leaves it.

    The framework groups a reasoning item with the function call it precedes, and a provider
    such as the Responses API refuses a reasoning item sent without that call. A record whose
    span holds one is not reshaped -- the reshape, like a merge, takes only spans holding nothing
    but the record -- and the written records the others become are inserted on group boundaries,
    never between a reasoning item and its call.
    """
    reasoning = Content.from_text_reasoning(text="I should record what I found.")
    messages = _history(6)
    reasoned = _record_pair(6)
    if shape == "preceding":
        reasoned.insert(0, Message(role="assistant", contents=[reasoning], message_id="think6"))
    else:
        reasoned[0].contents.insert(0, reasoning)
    later = _tool_group(200)
    later.insert(
        0,
        Message(
            role="assistant",
            contents=[Content.from_text_reasoning(text="Now look one more up.")],
            message_id="think200",
        ),
    )
    messages[-1:-1] = [*reasoned, *later]
    budget = _record_size(messages) - 200
    strategy = _strategy(_Summarizer(), budget=budget, share=None, policy="shed")
    reasoned_ids = {m.message_id for m in reasoned}

    await strategy(messages)

    sent = project_included_messages(messages)
    reasoning_sent = 0
    for position, message in enumerate(sent):
        if not any(c.type == "text_reasoning" for c in message.contents):
            continue
        reasoning_sent += 1
        carrier = message if any(c.type == "function_call" for c in message.contents) else None
        carrier = carrier or sent[position + 1]
        assert any(c.type == "function_call" for c in carrier.contents), (
            f"{message.message_id}'s reasoning sent without the call it precedes"
        )
    assert reasoning_sent == 2, "no reasoning item dropped with a reshaped record"
    assert strategy.records_reshaped == 6, "every record but the one holding reasoning"
    assert all(
        not m.additional_properties.get(EXCLUDED_KEY)
        for m in messages
        if m.message_id in reasoned_ids
    ), "the record with its reasoning item, whole"
    assert "rec6" in _included_record_calls(messages)


def _label(result: Content, shape: str) -> None:
    """Give a recall result the metadata or rich content ``shape`` names."""
    text = str(result.result)
    if shape == "labelled item":
        item = Content.from_text(text)
        item.additional_properties["security_label"] = {"confidentiality": "private"}
        result.items = [item]
    elif shape == "annotated item":
        result.items = [
            Content.from_text(
                text, annotations=[{"type": "citation", "url": "https://example.org/"}]
            )
        ]
    elif shape == "non-text item":
        result.items = [
            Content.from_text(text),
            Content.from_uri("https://example.org/chart.png", media_type="image/png"),
        ]
    elif shape == "annotated result":
        result.annotations = [Annotation(type="citation", url="https://example.org/")]
    elif shape == "provider result":
        result.raw_representation = {"provider": "state"}
    elif shape == "provider item":
        result.items = [Content.from_text(text, raw_representation={"provider": "state"})]
    elif shape == "different items":
        result.items = [Content.from_text("Additional text outside the compatibility result")]
    else:
        result.additional_properties["security_label"] = {"confidentiality": "private"}


@pytest.mark.parametrize("share", [None, 0.1])
@pytest.mark.parametrize(
    "shape",
    [
        "labelled item",
        "annotated item",
        "non-text item",
        "labelled result",
        "annotated result",
        "provider result",
        "provider item",
        "different items",
    ],
)
async def test_merges_and_rewrites_preserve_rich_records_between_plain_records(shape, share):
    messages = _history(9)
    protected = messages[10:12]
    result = protected[1].contents[0]
    _label(result, shape)
    before = result.to_dict()
    raw = copy.deepcopy(
        [result.raw_representation, *[c.raw_representation for c in result.items or []]]
    )
    _size(messages)
    assert len(consolidatable_record_groups(messages)) == 8
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=1100, share=share, cap=None)
    strategy.harder_attempts = 2

    with pytest.raises(ValueError, match="bare text"):
        strategy.tool_results.consolidate_records(messages, active_record_groups(messages), "short")
    assert not any(m.additional_properties.get(EXCLUDED_KEY) for m in messages)
    try:
        await strategy(messages)
    except CompactionBudgetExceeded:
        pass

    assert summarizer.merges
    assert strategy.records_merged > 0
    assert all(not m.additional_properties.get(EXCLUDED_KEY) for m in protected)
    assert result.to_dict() == before
    assert [result.raw_representation, *[c.raw_representation for c in result.items or []]] == raw
    assert "rec4" in _included_record_calls(messages)
    assert all("VAL-004" not in body for _, body in summarizer.requests)
    # Replaying the accepted merges must retain the same protected record.
    try:
        await strategy(messages)
    except CompactionBudgetExceeded:
        pass
    assert result.to_dict() == before
    assert "rec4" in _included_record_calls(messages)


@pytest.mark.parametrize(
    "shape", ["labelled result", "annotated item", "provider result", "non-text item"]
)
async def test_harder_rewrite_cannot_flatten_a_protected_record(shape):
    messages = _history(1)
    result = messages[3].contents[0]
    _label(result, shape)
    before = result.to_dict()
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=100, cap=None)
    strategy.harder_attempts = 2
    with pytest.raises(CompactionBudgetExceeded):
        await strategy(messages)
    assert not summarizer.requests
    assert result.to_dict() == before
    assert _included_record_calls(messages) == ["rec0"]


async def test_replayed_merge_skips_a_record_that_now_carries_metadata():
    original = _history(9)
    strategy = _strategy(_Summarizer(), budget=1100, cap=None)
    await strategy(copy.deepcopy(original))
    assert strategy.records_merged > 0
    result = original[11].contents[0]
    _label(result, "labelled result")
    before = result.to_dict()
    await strategy(original)
    assert result.to_dict() == before
    assert "rec4" in _included_record_calls(original)


@pytest.mark.parametrize(
    "shape", ["labelled item", "annotated item", "non-text item", "labelled result"]
)
async def test_a_record_carrying_metadata_is_not_reshaped_and_keeps_it(shape: str) -> None:
    """A written record is one bare text, so a result carrying more than its text stays as it is."""
    messages = _history(6)
    labelled = _record_pair(6)
    _label(labelled[1].contents[0], shape)
    before = labelled[1].contents[0].to_dict()
    messages[-1:-1] = labelled
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=_record_size(messages) - 200, share=None)

    await strategy(messages)

    assert strategy.records_reshaped == 6, "every record but the one carrying metadata"
    assert summarizer.requests == [], "and nothing merged it either"
    assert not any(m.additional_properties.get(EXCLUDED_KEY) for m in labelled)
    assert labelled[1].contents[0].to_dict() == before, "its metadata intact"
    assert "rec6" in _included_record_calls(messages)


async def test_a_batch_merged_behind_an_oversized_record_replays_on_rebuilt_history() -> None:
    """A kept merge that did not start at the oldest record is put back where it was made."""
    messages = _history(9)
    messages[2:4] = _record_pair(0, body=_body(0) + " and its full listing" * 300)
    original = copy.deepcopy(messages)
    budget = _record_size(copy.deepcopy(messages)) - 200
    summarizer = _Summarizer()
    first = _strategy(summarizer, budget=budget, share=0.25)

    await first(messages)

    assert first.records_merged >= 1, "the premise: a batch was merged"
    assert all("lookup_0 " not in body for body in summarizer.merges), (
        "the premise: the oversized oldest record shared no batch, so the merge was not leading"
    )
    asked = len(summarizer.requests)
    # A host whose larger budget leaves the rebuilt history under every trigger: only replay runs.
    cold = _strategy(summarizer, budget=budget * 1_000, share=0.25)
    cold.restore_session_state(json.loads(json.dumps(first.session_state())))
    rebuilt = copy.deepcopy(original)

    await cold(rebuilt)

    assert _sent(rebuilt) == _sent(messages)
    assert len(summarizer.requests) == asked, "replayed, not asked again"


def test_the_kept_transcript_index_answers_as_a_scan_of_every_transcript_would() -> None:
    """The sorted lookup agrees with checking every retained transcript, on near misses too."""
    transcripts = [
        "1. alpha\n2. beta",
        "1. alpha",
        "1. alphabet",
        "1. alp",
        "1. beta\n2. gamma",
        "1. b",
        "1. gamma",
        "1. \u00e9clair",
        "",
    ]
    heads = [
        "1. alpha",
        "1. alph",
        "1. alphabets",
        "1. b",
        "1. gamma",
        "1. beta",
        "1. betas",
        "1. \u00e9",
        "1. z",
        "",
        "2. alpha",
    ]
    ordered = sorted(transcripts)
    for head in heads:
        assert _has_prefix(ordered, head) == any(t.startswith(head) for t in transcripts), head


def _extra_output(call_id: str, message_id: str) -> Message:
    return Message(
        role="tool",
        contents=[{"type": "function_result", "call_id": call_id, "result": "late output"}],
        message_id=message_id,
    )


def _occurrence(occurrence: str, values: str) -> list[Message]:
    """Return a complete recall call and result under the shared service call id ``shared``."""
    return [
        Message(
            role="assistant",
            contents=[
                Content.from_function_call(
                    call_id="shared",
                    id=occurrence,
                    name=RECALL_TOOL_NAME,
                    arguments=json.dumps({"values": values}),
                )
            ],
            message_id=f"{occurrence}_call",
        ),
        Message(
            role="tool",
            contents=[
                Content.from_function_result(
                    call_id="shared", id=occurrence, result=make_recall_tool()(values)
                )
            ],
            message_id=f"{occurrence}_res",
        ),
    ]


def test_a_record_whose_call_id_another_output_answers_is_never_replaced() -> None:
    """Excluding the call would leave the later output under its id without a call."""
    messages = [
        Message(role="user", contents=["Look everything up."], message_id="task"),
        *_record_pair(0),
        Message(role="assistant", contents=["Noted."], message_id="between"),
        _extra_output("rec0", "late"),
        *_record_pair(1),
    ]
    annotate_message_groups(messages)
    standing = copy.deepcopy(messages)

    def ends(groups: list[dict[str, Any]]) -> list[str]:
        return [str(messages[group["end_index"]].message_id) for group in groups]

    assert ends(reshapeable_record_groups(messages)) == ["rec1_res"]
    assert ends(consolidatable_record_groups(messages)) == ["rec1_res"]
    (record,) = [group for group in active_record_groups(messages) if group["start_index"] == 1]
    strategy = _strategy(_Summarizer(), budget=10_000)
    with pytest.raises(ValueError, match="another output"):
        strategy.tool_results.consolidate_records(messages, [record], "replacement")
    with pytest.raises(ValueError, match="without loss"):
        strategy.tool_results.reshape_records(messages, [record])
    assert [m.to_dict() for m in messages] == [m.to_dict() for m in standing], "nothing mutated"


def test_sequential_complete_occurrences_sharing_a_call_id_stay_replaceable() -> None:
    """The control: each output answers its own occurrence, so neither record is held back."""
    messages = [
        Message(role="user", contents=["Look everything up."], message_id="task"),
        *_occurrence("first", _body(0)),
        Message(role="assistant", contents=["Noted."], message_id="between"),
        *_occurrence("second", _body(1)),
    ]
    annotate_message_groups(messages)

    assert len(reshapeable_record_groups(messages)) == 2
    assert len(consolidatable_record_groups(messages)) == 2


class _CountingTokenizer(CharacterEstimatorTokenizer):
    """Count the record transcripts measured: one for each batch prefix a sweep renders."""

    def __init__(self) -> None:
        super().__init__()
        self.transcripts = 0
        self.batches = 0

    def count_tokens(self, text: str) -> int:
        if '"role": "user"' in text and '"text": "1. ' in text:
            self.transcripts += 1
            self.batches += "\\n2. " in text
        return super().count_tokens(text)


def _padded_history(records: int, padding: int, *, plain_tail: int = 0) -> list[Message]:
    """Return records each padded by ``padding`` characters, then ``plain_tail`` unpadded ones."""
    messages = _history(records + plain_tail)
    for index in range(records):
        start = 2 + 2 * index
        messages[start : start + 2] = _record_pair(
            index, body=_body(index) + " and its full listing" * (padding // 21)
        )
    return messages


async def test_a_sweep_past_records_over_the_cap_renders_each_once_and_merges_a_later_batch() -> (
    None
):
    """A record whose own request exceeds the cap is passed over without a search from it."""
    tokenizer = _CountingTokenizer()
    messages = _padded_history(30, 5_500, plain_tail=2)
    budget = _record_size(copy.deepcopy(messages)) - 200
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=budget, share=0.25, tokenizer=tokenizer)

    await strategy(messages)

    (merged,) = summarizer.merges
    assert "lookup_30 " in merged and "lookup_31 " in merged, "the feasible batch at the end"
    assert strategy.records_merged == 1
    assert tokenizer.transcripts <= 2 * 32, f"{tokenizer.transcripts} transcripts rendered"


async def test_a_sweep_spends_at_most_its_probe_limit_when_no_two_records_share_a_batch() -> None:
    """Records that each fit alone and never in pairs cannot make every start scan every prefix."""
    tokenizer = _CountingTokenizer()
    messages = _padded_history(30, 2_400)
    budget = _record_size(copy.deepcopy(messages)) - 200
    strategy = _strategy(_Summarizer(), budget=budget, share=0.25, tokenizer=tokenizer)

    await strategy(messages)

    assert strategy.records_merged == 0, "the premise: no two records fit one request"
    assert tokenizer.transcripts <= 30 + _MAX_BATCH_PROBES, f"{tokenizer.transcripts} rendered"
    unbounded = sum(range(1, 30))
    assert _MAX_BATCH_PROBES < unbounded, "the limit is below every start scanning every prefix"


async def test_a_sweep_reads_the_history_once_and_again_only_after_an_accepted_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing over records costs no further walk of the history; only a merge changes it."""
    messages = _padded_history(30, 5_500, plain_tail=2)
    strategy = _strategy(
        _Summarizer(), budget=_record_size(copy.deepcopy(messages)) - 200, share=0.25
    )
    walks: Counter[str] = Counter()
    for name in ("consolidatable_record_groups", "record_bodies", "active_record_groups"):
        original = getattr(composed_module, name)

        def spy(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            walks[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(composed_module, name, spy)

    assert await strategy._merge_batches(messages, None, 0.25)

    assert strategy.records_merged == 1, "the premise: one accepted merge, after 30 records passed"
    # One survey, then for the accepted merge its own body check, its check for a kept
    # replacement and one survey of the history it changed: none for the records passed over.
    assert max(walks.values()) <= 1 + 3 * strategy.records_merged, dict(walks)


@pytest.mark.parametrize("probe_limit", [None, 8])
async def test_sweeps_past_records_that_never_pair_go_on_where_the_last_one_stopped(
    probe_limit: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start found not to pair is not searched again, even by a stack restored from state.

    With a limit of eight, the search from every start is cut off before it ends, so the start it
    was cut on has to be remembered too, or every sweep would stop on the same one.
    """
    if probe_limit is not None:
        monkeypatch.setattr(composed_module, "_MAX_BATCH_PROBES", probe_limit)
    messages = _padded_history(30, 2_400, plain_tail=2)
    budget = _record_size(copy.deepcopy(messages)) - 200
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=budget, share=0.25)

    for _ in range(120):
        if await strategy._merge_batches(messages, None, 0.25):
            break
        restored = _strategy(summarizer, budget=budget, share=0.25)
        restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
        strategy = restored

    (merged,) = summarizer.merges
    assert "lookup_30 " in merged, "the feasible batch past the run that never pairs"


class _PairsDoNotFitTokenizer(CharacterEstimatorTokenizer):
    """Count a two-record request far over the cap and a three-record one under it.

    Token counts need not grow steadily with text, and the batch search has to allow for it.
    """

    def count_tokens(self, text: str) -> int:
        payload = json.loads(text) if text.startswith("{") else None
        contents = (payload or {}).get("contents") or [{}]
        body = contents[0].get("text") or ""
        if (payload or {}).get("role") == "user" and body.startswith("1. "):
            records = len(re.findall(r"^\d+\. ", body, flags=re.MULTILINE))
            if records == 2:
                return 10 if "now pairs" in body else 100_000
            if records == 3:
                return 10
        return super().count_tokens(text)


async def test_an_appended_record_reopens_a_start_whose_pair_did_not_fit() -> None:
    """A start searched before a record was appended is searched again past what it covered."""
    tokenizer = _PairsDoNotFitTokenizer()
    summarizer = _Summarizer()
    messages = _history(2)
    first = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)

    assert not await first._merge_batches(messages, None, 0.001), "two records never fit"

    messages[-1:-1] = _record_pair(2)
    restored = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)
    restored.restore_session_state(json.loads(json.dumps(first.session_state())))

    assert await restored._merge_batches(messages, None, 0.001)
    (merged,) = summarizer.merges
    assert all(f"lookup_{index} " in merged for index in range(3)), "all three, as one batch"


async def test_a_changed_record_reopens_the_search_of_the_records_it_covered() -> None:
    """A search is resumed only past records that stand unchanged; a changed one restarts it."""
    tokenizer = _PairsDoNotFitTokenizer()
    summarizer = _Summarizer()
    messages = _history(2)
    first = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)
    assert not await first._merge_batches(messages, None, 0.001), "two records never fit"

    (result,) = (m for m in messages if m.message_id == "rec1_res")
    result.contents = [
        Content.from_function_result(
            call_id="rec1", result=make_recall_tool()(_body(1) + " and now pairs")
        )
    ]
    restored = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)
    restored.restore_session_state(json.loads(json.dumps(first.session_state())))

    assert await restored._merge_batches(messages, None, 0.001), "the changed pair now fits"


@pytest.mark.parametrize("records", [12, 24])
async def test_a_sweep_over_starts_it_already_searched_hashes_each_record_once(
    records: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking what a start searched against the records standing costs no hashing of them again."""
    tokenizer = _CountingTokenizer()
    messages = _padded_history(records, 2_400)
    strategy = _strategy(
        _Summarizer(),
        budget=_record_size(copy.deepcopy(messages)) - 200,
        share=0.25,
        tokenizer=tokenizer,
    )
    for _ in range(10):
        await strategy._merge_batches(messages, None, 0.25)
    hashed = 0
    original = composed_module._digest

    def counted(text: str) -> str:
        nonlocal hashed
        hashed += 1
        return original(text)

    monkeypatch.setattr(composed_module, "_digest", counted)
    tokenizer.batches = 0

    await strategy._merge_batches(messages, None, 0.25)

    assert tokenizer.batches == 0, "the premise: every start had searched every run from it"
    assert hashed <= 2 * records + 4, f"{hashed} digests for {records} records"


class _OnlyPairsFitTokenizer(CharacterEstimatorTokenizer):
    """Count a two-record request under the cap and any longer one far over it.

    Each such request is counted, and by the record it starts from.
    """

    def __init__(self) -> None:
        super().__init__()
        self.batches = 0
        self.starts: Counter[str] = Counter()

    def count_tokens(self, text: str) -> int:
        payload = json.loads(text) if text.startswith("{") else None
        contents = (payload or {}).get("contents") or [{}]
        body = contents[0].get("text") or ""
        if (payload or {}).get("role") == "user" and body.startswith("1. "):
            records = len(re.findall(r"^\d+\. ", body, flags=re.MULTILINE))
            if records >= 2:
                self.batches += 1
                self.starts[body.split()[1]] += 1
                return 10 if records == 2 else 100_000
        return super().count_tokens(text)


@pytest.mark.parametrize("probe_limit", [None, 8])
async def test_sweeps_past_refused_batches_go_on_where_the_last_one_stopped(
    probe_limit: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch refused before is passed over without searching for it again, even after a restore.

    Finding a refused pair again costs a sweep four prefixes, so a run of more of them than one
    sweep's limit covers would hold every later sweep short of the batch past it.
    """
    if probe_limit is not None:
        monkeypatch.setattr(composed_module, "_MAX_BATCH_PROBES", probe_limit)
    refused = composed_module._MAX_BATCH_PROBES // 4 + 8
    feasible = f"lookup_{2 * refused} "

    def merge(body: str) -> str:
        values = _VALUE.findall(body)
        return " ".join(sorted(set(values))) if feasible in body else values[0]

    tokenizer = _OnlyPairsFitTokenizer()
    summarizer = _Summarizer(merge=merge)
    messages = _history(2 * refused + 2)
    strategy = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)

    sweeps = 0
    while sweeps < 40 and not any(feasible in body for body in summarizer.merges):
        sweeps += 1
        await strategy._merge_batches(messages, None, 0.001)
        restored = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)
        restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
        strategy = restored

    assert summarizer.merges[-1].count(feasible) == 1, "the feasible batch past the refused run"
    assert len(summarizer.merges) == refused + 1, "each refused pair asked for once"
    # Four prefixes find a pair and one more measures its request; a sweep cut short spends at
    # most four on the pair it stops in. Finding a refused pair again would cost four each time.
    found_once = 5 * (refused + 1) + 4 * sweeps
    assert tokenizer.batches <= found_once, f"{tokenizer.batches} counted in {sweeps} sweeps"


class _UpToThreeFitTokenizer(CharacterEstimatorTokenizer):
    """Count a request of two or three records under the cap and any longer one far over it."""

    def count_tokens(self, text: str) -> int:
        payload = json.loads(text) if text.startswith("{") else None
        contents = (payload or {}).get("contents") or [{}]
        body = contents[0].get("text") or ""
        if (payload or {}).get("role") == "user" and body.startswith("1. "):
            records = len(re.findall(r"^\d+\. ", body, flags=re.MULTILINE))
            if records >= 2:
                return 10 if records <= 3 else 100_000
        return super().count_tokens(text)


async def test_a_record_appended_reopens_a_refused_batch_that_ran_to_the_last_record() -> None:
    """A batch the end of the records cut short is searched again once a record follows it."""
    summarizer = _Summarizer(
        merge=lambda body: (
            " ".join(sorted(set(_VALUE.findall(body))))
            if "lookup_2 " in body
            else _VALUE.findall(body)[0]
        )
    )
    tokenizer = _UpToThreeFitTokenizer()
    messages = _history(2)
    first = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)
    assert not await first._merge_batches(messages, None, 0.001), "the pair is refused"

    messages[-1:-1] = _record_pair(2)
    restored = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)
    restored.restore_session_state(json.loads(json.dumps(first.session_state())))

    assert await restored._merge_batches(messages, None, 0.001)
    refused, merged = summarizer.merges
    assert all(f"lookup_{index} " in merged for index in range(3)), "all three, as one batch"
    assert "lookup_2 " not in refused


async def test_a_changed_record_reopens_a_refused_batch_and_an_appended_one_does_not() -> None:
    """A refused batch is searched for again after a change to a record its search read, only.

    Each pair's search also read the two records after it, finding four too many. Appended
    records reopen only the searches the end of the records decided: here the last two pairs',
    which ran to the last record, and never the first two.
    """
    tokenizer = _OnlyPairsFitTokenizer()
    summarizer = _Summarizer(merge=lambda body: _VALUE.findall(body)[0])
    messages = _history(8)
    strategy = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)
    assert not await strategy._merge_batches(messages, None, 0.001)
    assert len(summarizer.merges) == 4, "the premise: four pairs, each refused"

    async def resweep() -> None:
        nonlocal strategy
        restored = _strategy(summarizer, budget=100_000, share=0.001, tokenizer=tokenizer)
        restored.restore_session_state(json.loads(json.dumps(strategy.session_state())))
        strategy = restored
        tokenizer.starts.clear()
        assert not await strategy._merge_batches(messages, None, 0.001)

    messages[-1:-1] = _record_pair(8) + _record_pair(9)
    await resweep()
    assert len(summarizer.merges) == 5, "only the appended pair is asked for"
    assert set(tokenizer.starts) == {"lookup_4", "lookup_6", "lookup_8"}

    (result,) = (m for m in messages if m.message_id == "rec2_res")
    result.contents = [
        Content.from_function_result(
            call_id="rec2", result=make_recall_tool()(_body(2).replace("VAL-002-3", "VAL-002-9"))
        )
    ]
    await resweep()
    assert len(summarizer.merges) == 6, "the changed pair, asked for again"
    assert "VAL-002-9" in summarizer.merges[-1]
    assert set(tokenizer.starts) == {"lookup_0", "lookup_2"}, "the searches that read record 2"


def test_a_search_memory_from_an_earlier_snapshot_shape_restores() -> None:
    """Searches that found nothing restore from the earlier key; an unknown entry shape is dropped."""
    strategy = _strategy(_Summarizer(), budget=100_000)
    state = strategy.session_state()
    state.pop("searched_records")
    state["unpaired_records"] = [["start", 900, 3, "run"], ["first", "second", 900]]

    strategy.restore_session_state(json.loads(json.dumps(state)))

    assert strategy.session_state()["searched_records"] == [["start", 900, 3, "run", 0, False]]
