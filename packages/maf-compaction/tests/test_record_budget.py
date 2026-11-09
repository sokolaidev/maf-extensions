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
from typing import Any, Literal

import pytest
from agent_framework import CharacterEstimatorTokenizer, ChatResponse, Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
    project_included_messages,
)

from maf_compaction._composed import (
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
    """A summarizer that answers merges by ``merge`` and anything else with one short line."""

    def __init__(self, merge: Any = None) -> None:
        self.merge = merge or (lambda body: " ".join(sorted(set(_VALUE.findall(body)))))
        self.requests: list[tuple[str, str]] = []

    @property
    def merges(self) -> list[str]:
        return [body for prompt, body in self.requests if prompt == DEFAULT_RECORD_MERGE_PROMPT]

    async def get_response(
        self, messages: list[Message], *, stream: bool = False, **kwargs: Any
    ) -> ChatResponse:
        prompt, body = messages[0].text or "", messages[-1].text or ""
        self.requests.append((prompt, body))
        text = self.merge(body) if prompt == DEFAULT_RECORD_MERGE_PROMPT else "Short summary."
        return ChatResponse(messages=[Message(role="assistant", contents=[text])])


def _strategy(
    summarizer: _Summarizer,
    *,
    budget: int,
    share: float | None = DEFAULT_RECORD_BUDGET_SHARE,
    cap: int | None = _CAP,
    policy: Literal["shed", "raise"] = "raise",
) -> ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy:
    """Return the composed row with a tiny head and tail, and its record half never waiting."""
    return ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=TOKENIZER,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=budget,
            tokenizer=TOKENIZER,
            keep_head_groups=1,
            keep_tail_groups=1,
            trigger_fraction=0.01,
            fallback_fraction=0.99,
        ),
        user_turns=UserTurnAnchoredSummarizationCompactionStrategy(
            max_input_tokens=budget,
            tokenizer=TOKENIZER,
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
    else:
        result.additional_properties["security_label"] = {"confidentiality": "private"}


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
