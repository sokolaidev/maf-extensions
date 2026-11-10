"""Completed recall reasoning leaves atomically without editing retained reasoning's prefix."""

from __future__ import annotations

import copy
import json

import pytest
from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_message_groups,
    project_included_messages,
)
from test_record_budget import (
    _history,
    _record_pair,
    _sent,
    _sent_values,
    _strategy,
    _Summarizer,
    _tool_group,
)

from maf_compaction._composed import DEFAULT_RECORD_MERGE_PROMPT, _transcript, harder_record_prompt
from maf_compaction._request import RECALL_PROMPT_KEY
from maf_compaction._toolsummary import (
    active_record_groups,
    consolidatable_record_groups,
    reshapeable_record_groups,
)


def _reasoned(records: int, shape: str = "preceding") -> list[Message]:
    messages = _history(records)
    for index in reversed(range(records)):
        reasoning = Content.from_text_reasoning(text="Keep these facts.")
        reasoning.additional_properties["signature"] = f"signature-{index}"
        if shape == "preceding":
            messages.insert(
                2 + index * 2,
                Message(role="assistant", contents=[reasoning], message_id=f"think{index}"),
            )
        else:
            messages[2 + index * 2].contents.insert(0, reasoning)
    annotate_message_groups(messages)
    return messages


@pytest.mark.parametrize("shape", ["preceding", "co-located"])
@pytest.mark.parametrize("later_reasoning", [False, True])
def test_completed_reasoning_reshapes_losslessly_and_replays(
    shape: str, later_reasoning: bool
) -> None:
    messages = _reasoned(4, shape)
    original = copy.deepcopy(messages)
    values = _sent_values(messages)
    strategy = _strategy(_Summarizer(), budget=100_000)

    assert strategy._reshape_records(messages)
    assert strategy.records_reshaped == 4
    assert _sent_values(messages) == values
    assert not any(
        content.type in {"text_reasoning", "function_call", "function_result"}
        for message in project_included_messages(messages)
        for content in message.contents
    )
    if later_reasoning:
        later = _tool_group(20)
        later[0].contents.insert(
            0, Content.from_text_reasoning(text="Reasoned on compacted input.")
        )
        messages.extend(later)
        original.extend(copy.deepcopy(later))
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert cold._reshape_records(original, replay=True)
    assert cold.records_reshaped == 0
    assert _sent(original) == _sent(messages)
    assert not cold._reshape_records(original, replay=True)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["merge", "rewrite"])
@pytest.mark.parametrize("later_reasoning", [False, True])
async def test_completed_reasoning_can_be_consolidated_and_replayed(
    operation: str,
    later_reasoning: bool,
) -> None:
    messages = _reasoned(1)
    if operation == "merge":
        messages[-1:-1] = _record_pair(1)
        annotate_message_groups(messages)
    original = copy.deepcopy(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text, rewrite=lambda _: text), budget=100_000)
    groups = consolidatable_record_groups(messages)
    assert len(groups) == (2 if operation == "merge" else 1)
    outcome = await strategy._consolidate(
        messages,
        groups,
        prompt=DEFAULT_RECORD_MERGE_PROMPT if operation == "merge" else harder_record_prompt(1),
        transcript=_transcript(messages, groups),
    )
    assert outcome == "accepted"
    assert all(
        message.additional_properties.get(EXCLUDED_KEY)
        for message in messages
        if message.message_id in {"think0", "rec0_call", "rec0_res"}
    )
    assert not any(
        content.type == "text_reasoning"
        for message in project_included_messages(messages)
        for content in message.contents
    )
    assert _sent_values(messages) == _sent_values(original)
    if later_reasoning:
        later = _tool_group(20)
        later[0].contents.insert(
            0, Content.from_text_reasoning(text="Reasoned on compacted input.")
        )
        messages.extend(later)
        original.extend(copy.deepcopy(later))
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert cold._keep_decisions(original)
    assert _sent(original) == _sent(messages)


@pytest.mark.parametrize(
    "boundary", ["none", "tool_result", "recall_prompt", "summary", "excluded"]
)
def test_active_tool_loop_and_synthetic_users_do_not_complete_a_record(boundary: str) -> None:
    messages = _reasoned(1)[:-1]
    if boundary != "none":
        user = Message(role="user", contents=["Synthetic boundary"], message_id="fake")
        if boundary == "tool_result":
            user.contents = [Content.from_function_result(call_id="other", result="result")]
        elif boundary == "recall_prompt":
            user.additional_properties[RECALL_PROMPT_KEY] = True
        elif boundary == "summary":
            user.additional_properties[GROUP_ANNOTATION_KEY] = {
                SUMMARY_OF_MESSAGE_IDS_KEY: ["old-user"],
                SUMMARY_OF_GROUP_IDS_KEY: ["old-group"],
            }
        else:
            user.additional_properties[EXCLUDED_KEY] = True
        messages.append(user)
    annotate_message_groups(messages)
    before = _sent(messages)
    groups = active_record_groups(messages)
    assert groups
    assert not consolidatable_record_groups(messages)
    assert not reshapeable_record_groups(messages)
    strategy = _strategy(_Summarizer(), budget=100_000)
    with pytest.raises(ValueError):
        strategy.tool_results.consolidate_records(messages, groups, "replacement")
    with pytest.raises(ValueError):
        strategy.tool_results.reshape_records(messages, groups)
    assert _sent(messages) == before


def test_later_retained_reasoning_protects_its_entire_record_prefix() -> None:
    messages = _reasoned(2)
    later = _tool_group(20)
    later[0].contents.insert(0, Content.from_text_reasoning(text="A later decision."))
    messages[-1:-1] = later
    annotate_message_groups(messages)
    before = _sent(messages)
    assert not consolidatable_record_groups(messages)
    assert not reshapeable_record_groups(messages)
    strategy = _strategy(_Summarizer(), budget=100_000)
    assert not strategy._reshape_records(messages)
    with pytest.raises(ValueError):
        strategy.tool_results.consolidate_records(messages, active_record_groups(messages), "short")
    assert _sent(messages) == before


def test_current_carried_result_still_protects_completed_reasoning() -> None:
    messages = _reasoned(1)
    for message in messages:
        if message.message_id != "rec0_res":
            message.additional_properties["_attribution"] = "history"
    assert not consolidatable_record_groups(messages)
    assert not reshapeable_record_groups(messages)


@pytest.mark.parametrize("extra", ["text", "call", "result"])
def test_completed_reasoning_does_not_license_unrelated_content(extra: str) -> None:
    messages = _reasoned(1, "co-located")
    call = messages[2]
    if extra == "text":
        call.contents.append(Content.from_text("Separate assistant output"))
    elif extra == "call":
        other = _tool_group(20)
        call.contents.extend(other[0].contents)
        messages[3].contents.extend(other[1].contents)
    else:
        messages[3].contents.extend(_record_pair(0)[1].contents)
    annotate_message_groups(messages)
    assert not consolidatable_record_groups(messages)
    assert not reshapeable_record_groups(messages)
