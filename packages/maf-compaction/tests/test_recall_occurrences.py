"""Recall authority follows local call occurrences across persisted histories."""

import copy

import pytest
from agent_framework import CharacterEstimatorTokenizer, Content, Message
from agent_framework._compaction import EXCLUDED_KEY, annotate_message_groups, group_messages

from maf_compaction._preserve import removable_whole
from maf_compaction._toolsummary import (
    RECALL_TOOL_NAME,
    RECORD_MARKER,
    ToolResultAnchoredSummarizationCompactionStrategy,
    _is_recall_group,
    _newest_record_identity,
    active_record_groups,
    consolidatable_record_groups,
    find_record_index,
    record_body,
    record_text,
)


def call(occurrence, *, name=RECALL_TOOL_NAME, hosted=False, message_id=None):
    return Message(
        "assistant",
        [
            Content.from_function_call(
                call_id="shared",
                name=name,
                arguments="{}",
                id=occurrence,
                hosted=hosted,
            )
        ],
        message_id=message_id,
    )


def result(occurrence, *, value="CODE-123"):
    return Message(
        "tool",
        [
            Content.from_function_result(
                call_id="shared",
                result=f"{RECORD_MARKER} {value}",
                id=occurrence,
            )
        ],
    )


@pytest.mark.parametrize("reload", [False, True])
def test_unrelated_reused_call_id_cannot_advance_or_supply_record_coverage(reload):
    messages = [
        call("recall"),
        result("recall"),
        Message("user", "Continue."),
        call("other", name="lookup"),
        result("other", value="SECRET-999"),
    ]
    if reload:
        messages = [Message.from_dict(message.to_dict()) for message in messages]
    assert find_record_index(messages) == 1
    assert _newest_record_identity(messages) == "occurrence:recall"
    assert record_text(messages[-1], history=messages) == ""
    annotate_message_groups(messages)
    records = active_record_groups(messages)
    assert len(records) == 1
    assert "CODE-123" in record_body(messages, records[0])
    assert "SECRET-999" not in record_body(messages, records[0])
    assert not _is_recall_group(messages, group_messages(messages)[-1])


def test_newest_record_identity_survives_copy_reload_and_prefix_projection():
    first = [call("first"), result("first")]
    second = [call("second"), result("second")]
    whole = first + second
    assert _newest_record_identity(first) != _newest_record_identity(whole)
    expected = "occurrence:second"
    assert _newest_record_identity(whole) == expected
    assert _newest_record_identity(second) == expected
    assert _newest_record_identity(copy.deepcopy(whole)) == expected
    assert _newest_record_identity([Message.from_dict(m.to_dict()) for m in whole]) == expected


@pytest.mark.parametrize("result_id", [None, "missing", "other"])
def test_ambiguous_or_mismatched_results_do_not_establish_a_record(result_id):
    messages = [call("recall"), call("other", name="lookup"), result(result_id)]
    assert find_record_index(messages) is None
    assert _newest_record_identity(messages) == ""
    assert record_text(messages[-1], history=messages) == ""


def test_explicit_occurrence_disambiguates_interleaved_results():
    messages = [call("recall"), call("other", name="lookup"), result("other"), result("recall")]
    assert find_record_index(messages) == 3
    assert record_text(messages[2], history=messages) == ""
    assert "CODE-123" in record_text(messages[3], history=messages)


def test_legacy_result_binds_only_to_the_remaining_unanswered_call():
    messages = [call("other", name="lookup"), result("other"), call("recall"), result(None)]
    assert find_record_index(messages) == 3
    assert _newest_record_identity(messages) == "occurrence:recall"


@pytest.mark.parametrize("hosted", [False, True])
def test_hosted_calls_do_not_authorize_local_recall_records(hosted):
    messages = [call("recall", hosted=hosted), result("recall")]
    assert find_record_index(messages) == (None if hosted else 1)
    if hosted:
        assert active_record_groups(messages) == []


def test_result_before_declaration_and_duplicate_result_are_not_records():
    messages = [result("recall"), call("recall"), result("recall"), result("recall")]
    assert find_record_index(messages) == 2
    assert record_text(messages[0], history=messages) == ""
    assert record_text(messages[-1], history=messages) == ""


def test_batched_non_recall_marker_does_not_supply_coverage_or_identity():
    messages = [call("other", name="lookup"), call("recall")]
    messages.append(
        Message(
            "tool",
            [
                result("other", value="SECRET-999").contents[0],
                result("recall").contents[0],
            ],
        )
    )
    assert find_record_index(messages) == 2
    assert _newest_record_identity(messages) == "occurrence:recall"
    text = record_text(messages[-1], history=messages)
    assert "CODE-123" in text and "SECRET-999" not in text


def test_legacy_message_identity_distinguishes_repeated_service_ids():
    first = [call(None, message_id="first"), result(None)]
    second = [call(None, message_id="second"), result(None)]
    assert _newest_record_identity(first) != _newest_record_identity(first + second)
    assert _newest_record_identity(first + second) == _newest_record_identity(second)


def test_newest_record_identity_advances_within_a_batched_result():
    messages = [call("first"), call("second"), result("first")]
    before = _newest_record_identity(messages)
    messages[-1].contents.append(result("second").contents[0])
    assert _newest_record_identity(messages) != before
    assert _newest_record_identity(messages) == "occurrence:second"


def test_repeated_unidentified_legacy_calls_fail_closed():
    messages = [call(None), result(None), call(None), result(None)]
    assert find_record_index(messages) is None
    annotate_message_groups(messages)
    assert find_record_index(messages) is None
    assert find_record_index([Message.from_dict(m.to_dict()) for m in messages]) is None


def test_legacy_identity_survives_independently_grouped_projection():
    messages = [
        Message("user", "Earlier"),
        Message("assistant", "Answer"),
        call(None),
        result(None),
    ]
    tail = copy.deepcopy(messages[2:])
    expected = _newest_record_identity(messages)
    annotate_message_groups(messages)
    annotate_message_groups(tail)
    assert _newest_record_identity(messages) == expected
    assert _newest_record_identity(tail) == expected


@pytest.mark.parametrize("reload", [False, True])
def test_sequential_idless_results_leave_complete_occurrences_removable(reload):
    messages = [call("first"), result(None), Message("user", "More"), call("second"), result(None)]
    if reload:
        messages = [Message.from_dict(m.to_dict()) for m in messages]
    annotate_message_groups(messages)
    spans = group_messages(messages)
    assert all(removable_whole(messages, spans, span) for span in spans)
    records = consolidatable_record_groups(messages)
    assert len(records) == 2
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=1000, tokenizer=CharacterEstimatorTokenizer()
    )
    original = list(messages)
    strategy.consolidate_records(messages, records, "CODE-123")
    assert all(original[i].additional_properties[EXCLUDED_KEY] for i in (0, 1, 3, 4))
    assert not original[2].additional_properties.get(EXCLUDED_KEY, False)


def test_a_batch_containing_only_matched_recall_results_can_be_consolidated():
    messages = [
        Message("assistant", [call("first").contents[0], call("second").contents[0]]),
        Message("tool", [result("first").contents[0], result("second").contents[0]]),
    ]
    annotate_message_groups(messages)
    assert len(consolidatable_record_groups(messages)) == 1


def test_framework_shaped_caller_ids_do_not_supply_legacy_occurrence_authority():
    messages = [
        call(None, message_id="msg_7"),
        result(None),
        call(None, message_id="msg_8"),
        result(None),
    ]
    assert find_record_index(messages) is None


def test_ambiguous_idless_result_does_not_allow_partial_removal():
    messages = [call("first"), Message("user", "More"), call("second"), result(None)]
    annotate_message_groups(messages)
    spans = group_messages(messages)
    assert not removable_whole(messages, spans, spans[0])
    assert not removable_whole(messages, spans, spans[-1])


def test_user_authored_declaration_cannot_establish_a_recall_record():
    declaration = call("recall")
    declaration.role = "user"
    assert find_record_index([declaration, result("recall")]) is None


def test_duplicate_occurrence_ids_fail_closed():
    messages = [call("duplicate"), result("duplicate"), call("duplicate"), result("duplicate")]
    assert find_record_index(messages) is None


def test_interleaved_occurrences_cannot_be_removed_separately():
    messages = [
        call("first"),
        Message("user", "More"),
        call("second"),
        result("first"),
        Message("user", "Continue"),
        result("second"),
    ]
    annotate_message_groups(messages)
    spans = group_messages(messages)
    for span in spans:
        members = messages[span["start_index"] : span["end_index"] + 1]
        calls = {c.id for m in members for c in m.contents if c.type == "function_call"}
        results = {c.id for m in members for c in m.contents if c.type == "function_result"}
        if calls != results:
            assert not removable_whole(messages, spans, span)


def test_complete_repeated_occurrences_remain_removable():
    messages = [
        call("first"),
        result("first"),
        Message("user", "More"),
        call("second"),
        result("second"),
    ]
    annotate_message_groups(messages)
    spans = group_messages(messages)
    assert all(removable_whole(messages, spans, span) for span in spans)


@pytest.mark.anyio
async def test_unrelated_batched_marker_cannot_authorize_dropping_unrecorded_work():
    messages = [
        Message("user", "Find facts"),
        call("lookup", name="lookup"),
        result("lookup", value="SECRET-999"),
        Message("user", "Record the facts"),
        call("unrelated", name="other"),
        call("recall"),
        Message(
            "tool",
            [result("unrelated", value="SECRET-999").contents[0], result("recall").contents[0]],
        ),
    ]
    messages[2].contents[0] = Content.from_function_result(
        call_id="shared",
        id="lookup",
        result="SECRET-999 " + "payload " * 1000,
    )
    strategy = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=20_000,
        tokenizer=CharacterEstimatorTokenizer(),
        trigger_fraction=0.01,
        keep_head_groups=0,
    )
    await strategy(messages)
    assert not messages[2].additional_properties.get(EXCLUDED_KEY, False)
    assert "payload " * 1000 in messages[2].contents[0].result
    assert strategy.groups_kept_uncovered >= 1
