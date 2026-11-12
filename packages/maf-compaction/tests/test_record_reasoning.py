"""Completed recall reasoning leaves atomically without editing retained reasoning's prefix."""

from __future__ import annotations

import copy
import json

import pytest
from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_message_groups,
    group_messages,
    project_included_messages,
)
from test_record_budget import (
    _history,
    _record_pair,
    _sent,
    _sent_values,
    _size,
    _strategy,
    _Summarizer,
    _tool_group,
)

from maf_compaction import _toolsummary as toolsummary_module
from maf_compaction._anchored import _is_marker
from maf_compaction._composed import DEFAULT_RECORD_MERGE_PROMPT, _transcript, harder_record_prompt
from maf_compaction._request import RECALL_PROMPT_KEY
from maf_compaction._toolsummary import (
    active_record_groups,
    consolidatable_record_groups,
    reshapeable_record_groups,
)
from maf_compaction._usersummary import _is_summary


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


def _hold_later_user_edits(
    original: list[Message], messages: list[Message], *, include_records: bool = False
) -> None:
    held = {message.message_id for message in original}
    for index, message in enumerate(messages):
        if message.message_id in held or not (
            _is_summary(message)
            or _is_marker(message)
            or (include_records and toolsummary_module._is_written_record(message))
        ):
            continue
        following = next(
            (later.message_id for later in messages[index + 1 :] if later.message_id in held),
            None,
        )
        insertion = next(
            (i for i, item in enumerate(original) if item.message_id == following), len(original)
        )
        original.insert(insertion, message)
        held.add(message.message_id)


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    "operation", ["reshape", "merge", "rewrite", "reshape-merge", "merge-rewrite"]
)
@pytest.mark.parametrize("shape", ["preceding", "co-located"])
@pytest.mark.parametrize("all_flags", [False, True])
async def test_flagless_record_replacements_replay_before_retained_reasoning(
    operation: str, shape: str, all_flags: bool
) -> None:
    messages = _reasoned(1 if operation == "rewrite" else 3, shape)
    originals = {m.message_id for m in messages}
    text = " ".join(sorted(_sent_values(messages)))
    answers = iter([text + " Additional details." * 20, text])
    strategy = _strategy(_Summarizer(merge=lambda _: next(answers)), budget=100_000, cap=None)
    if operation.startswith("reshape"):
        assert strategy._reshape_records(messages)
    if operation != "reshape":
        for _ in range(2 if operation == "merge-rewrite" else 1):
            groups = consolidatable_record_groups(messages)
            assert (
                await strategy._consolidate(
                    messages,
                    groups,
                    prompt=DEFAULT_RECORD_MERGE_PROMPT,
                    transcript=_transcript(messages, groups),
                )
                == "accepted"
            )
    _retain_later_reasoning(messages)
    expected = _sent(messages)
    flagless = copy.deepcopy(messages)
    for message in flagless:
        if all_flags or message.message_id in originals:
            message.additional_properties.pop(EXCLUDED_KEY, None)
    cold = _strategy(_Summarizer(), budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert cold._replay_record_edits(flagless)
    assert _sent(flagless) == expected
    assert _sent(messages) == expected
    assert len(flagless) == len(messages)
    assert not cold._replay_record_edits(flagless)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "merge", "rewrite"])
@pytest.mark.parametrize(
    "tamper", ["body", "metadata", "identity", "duplicate", "moved", "excluded", "preserved"]
)
async def test_invalid_held_record_cannot_authorize_flagless_replay(
    operation: str, tamper: str
) -> None:
    from maf_compaction._preserve import STANDS_IN_KEY, set_preserved
    from maf_compaction._record_replay import LINEAGE_KEY

    messages = _reasoned(1 if operation == "rewrite" else 3)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000, cap=None)
    if operation == "reshape":
        assert strategy._reshape_records(messages)
    else:
        groups = consolidatable_record_groups(messages)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    for message in messages:
        message.additional_properties.pop(EXCLUDED_KEY, None)
    held = [m for m in messages if toolsummary_module._is_written_record(m)][-1]
    if tamper == "body":
        held.contents[0].text += " Changed."
    elif tamper == "metadata":
        held.contents[0].additional_properties["provider_state"] = "protected"
    elif tamper == "identity":
        identity_key = STANDS_IN_KEY if operation == "reshape" else LINEAGE_KEY
        held.additional_properties[identity_key] = "different-source"
    elif tamper == "duplicate":
        messages.insert(messages.index(held), copy.deepcopy(held))
    elif tamper == "moved":
        messages.remove(held)
        messages.insert(2, held)
    elif tamper == "excluded":
        held.additional_properties[EXCLUDED_KEY] = True
        held.additional_properties[EXCLUDE_REASON_KEY] = "host"
    else:
        set_preserved(held, preserved=True, reason="host")
    _retain_later_reasoning(messages)
    cold = _strategy(_Summarizer(), budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    before = _sent(messages)
    assert not cold._replay_record_edits(messages)
    assert _sent(messages) == before


@pytest.mark.anyio
async def test_flagless_merge_replays_after_original_recall_sources_were_pruned() -> None:
    messages = _reasoned(3)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000, cap=None)
    assert strategy._reshape_records(messages)
    messages[:] = [m for m in messages if not m.additional_properties.get(EXCLUDED_KEY)]
    annotate_message_groups(messages)
    groups = consolidatable_record_groups(messages)
    assert (
        await strategy._consolidate(
            messages,
            groups,
            prompt=DEFAULT_RECORD_MERGE_PROMPT,
            transcript=_transcript(messages, groups),
        )
        == "accepted"
    )
    _retain_later_reasoning(messages)
    expected = _sent(messages)
    for message in messages:
        message.additional_properties.pop(EXCLUDED_KEY, None)
    cold = _strategy(_Summarizer(), budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert cold._replay_record_edits(messages)
    assert _sent(messages) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "merge"])
@pytest.mark.parametrize("later_edit", ["fold", "shed", "fold-and-shed"])
@pytest.mark.parametrize(
    "history_kind",
    [
        "original",
        "shared",
        "serialized-shared",
        "changed-shared",
        "held-shared",
        "held-later",
        "serialized-held-later",
        "held-later-no-summaries",
        "changed-shared-held-later",
        "held-shared-held-later",
        "held-later-no-sources",
        "changed-record-held-later-no-summaries",
    ],
)
async def test_record_replay_preserves_intermediate_edit_windows(
    operation: str, later_edit: str, history_kind: str
) -> None:
    messages = _history(2)
    middle = [
        Message("user", ["First facts. " * 100], message_id="middle0"),
        Message("assistant", ["Intervening narration."], message_id="narration"),
        Message("user", ["Second facts. " * 100], message_id="middle1"),
    ]
    messages[4:4] = middle
    original = copy.deepcopy(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000, cap=None)
    _size(messages)
    for index in range(2):
        group = next(
            group
            for group in group_messages(messages)
            if messages[group["start_index"]].message_id == f"middle{index}"
        )
        strategy.user_turns._replace(
            messages,
            [group],
            f"Summary {index}. " * 40,
            summary_id=f"summary_{index}",
        )
    if history_kind != "original":
        original = list(messages)
    if operation == "reshape":
        assert strategy._reshape_records(messages)
    else:
        groups = consolidatable_record_groups(messages)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    if "fold" in later_edit:
        assert await strategy.user_turns.fold_if_smaller(messages) == "folded"
    if "shed" in later_edit:
        strategy._shed_ids.add("narration")
        assert strategy._keep_shedding(messages)
    if "held-later" in history_kind:
        _hold_later_user_edits(original, messages)
        if history_kind.endswith("held-later-no-summaries") and "fold" in later_edit:
            original[:] = [m for m in original if m.message_id not in {"summary_0", "summary_1"}]
        if history_kind == "held-later-no-sources":
            original[:] = [
                m
                for m in original
                if m.message_id not in {"summary_0", "summary_1", "middle0", "middle1"}
            ]
    for history in (messages, original):
        _retain_later_reasoning(history)
    expected = _sent(messages)
    if history_kind in {"serialized-shared", "serialized-held-later"}:
        original = [
            Message.from_dict(item)
            for item in json.loads(json.dumps([m.to_dict() for m in original]))
        ]
    cold = _strategy(_Summarizer(), budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    if history_kind == "changed-record-held-later-no-summaries":
        original = [copy.deepcopy(m) if m.message_id == "rec0_res" else m for m in original]
        next(m for m in original if m.message_id == "rec0_res").contents[0].result += " changed"
        before = _sent(original)
        assert not cold._replay_record_edits(original)
        assert cold.user_summaries_replayed == 0
        assert _sent(original) == before
        assert _sent(messages) == expected
        return
    if history_kind == "held-later-no-sources":
        before = _sent(original)
        assert not cold._replay_record_edits(original)
        assert _sent(original) == before
        assert _sent(messages) == expected
        return
    if history_kind.startswith(("changed-shared", "held-shared")):
        original = [copy.deepcopy(m) if m.message_id == "summary_0" else m for m in original]
        summary = next(m for m in original if m.message_id == "summary_0")
        if history_kind.startswith("changed-shared"):
            summary.contents[0].text += " changed"
        else:
            summary.additional_properties[EXCLUDED_KEY] = True
            summary.additional_properties[EXCLUDE_REASON_KEY] = "external-owner"
        before = _sent(original)
        assert not cold._replay_record_edits(original)
        assert _sent(original) == before
        assert _sent(messages) == expected
        return
    assert await cold(original)
    assert _sent(original) == expected
    assert _sent(messages) == expected
    assert not cold._replay_record_edits(original)
    assert _sent(original) == expected
    assert _sent(messages) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "merge"])
@pytest.mark.parametrize("serialized", [False, True])
async def test_replay_preserves_later_held_summary_outside_record_window(
    operation: str, serialized: bool
) -> None:
    messages = _history(2)
    messages.insert(-1, Message("user", ["Later facts. " * 100], message_id="later-user"))
    original = list(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000, cap=None)
    if operation == "reshape":
        assert strategy._reshape_records(messages)
    else:
        groups = consolidatable_record_groups(messages)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    group = next(
        g for g in group_messages(messages) if messages[g["start_index"]].message_id == "later-user"
    )
    strategy.user_turns._replace(messages, [group], "Later facts.", summary_id="later-summary")
    summary = next(m for m in messages if m.message_id == "later-summary")
    original.insert(
        next(i for i, m in enumerate(original) if m.message_id == "later-user"), summary
    )
    for history in (messages, original):
        _retain_later_reasoning(history)
    expected = _sent(messages)
    if serialized:
        original = [
            Message.from_dict(m) for m in json.loads(json.dumps([m.to_dict() for m in original]))
        ]
    cold = _strategy(_Summarizer(), budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert await cold(original)
    assert _sent(original) == expected
    assert _sent(messages) == expected
    assert not cold._replay_record_edits(original)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "merge"])
@pytest.mark.parametrize("folds", [0, 1, 2, 3])
@pytest.mark.parametrize("keep_intermediate", [False, True])
@pytest.mark.parametrize("record_timing", ["before", "between", "after"])
@pytest.mark.parametrize("hold_records", [False, True])
async def test_replay_restores_held_prerequisites_through_successive_folds(
    operation: str, folds: int, keep_intermediate: bool, record_timing: str, hold_records: bool
) -> None:
    messages = _history(2)
    messages[4:4] = [
        Message("user", ["First facts. " * 100], message_id="middle0"),
        Message("assistant", ["Narration."], message_id="narration"),
        Message("user", ["Second facts. " * 100], message_id="middle1"),
    ]
    original = copy.deepcopy(messages)
    text = " ".join(sorted(_sent_values(messages)))
    answers = iter(text + " Additional details." * count for count in (40, 30, 20, 10, 0))
    strategy = _strategy(
        _Summarizer(merge=lambda _: next(answers) if record_timing == "between" else text),
        budget=100_000,
        cap=None,
    )
    _size(messages)
    for index in range(2):
        group = next(
            g
            for g in group_messages(messages)
            if messages[g["start_index"]].message_id == f"middle{index}"
        )
        strategy.user_turns._replace(
            messages, [group], f"Summary {index}. " * 40, summary_id=f"summary_{index}"
        )

    async def edit_records(*, reshape: bool) -> None:
        if reshape:
            assert strategy._reshape_records(messages)
        else:
            groups = consolidatable_record_groups(messages)
            assert (
                await strategy._consolidate(
                    messages,
                    groups,
                    prompt=DEFAULT_RECORD_MERGE_PROMPT,
                    transcript=_transcript(messages, groups),
                )
                == "accepted"
            )

    if record_timing != "after":
        await edit_records(reshape=operation == "reshape")
    for index in range(folds):
        if index:
            user = Message("user", ["Additional facts. " * 100], message_id=f"extra{index}")
            messages.insert(-1, user)
            original.insert(-1, copy.deepcopy(user))
            _size(messages)
            group = next(
                g
                for g in group_messages(messages)
                if messages[g["start_index"]].message_id == user.message_id
            )
            strategy.user_turns._replace(
                messages,
                [group],
                f"Extra summary {index}. " * 40,
                summary_id=f"extra_summary_{index}",
            )
        assert await strategy.user_turns.fold_if_smaller(messages) == "folded"
        if record_timing == "between":
            await edit_records(reshape=False)
    if record_timing == "after":
        await edit_records(reshape=operation == "reshape")
    retained = [
        m
        for m in messages
        if keep_intermediate or not (_is_summary(m) and m.additional_properties.get(EXCLUDED_KEY))
    ]
    _hold_later_user_edits(original, retained, include_records=hold_records)
    for history in (messages, original):
        _retain_later_reasoning(history)
    expected = _sent(messages)
    cold = _strategy(_Summarizer(), budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert await cold(original)
    assert _sent(original) == expected
    assert _sent(messages) == expected
    assert not cold._replay_record_edits(original)


@pytest.mark.anyio
@pytest.mark.parametrize("shed_first", [False, True])
@pytest.mark.parametrize("durable_shed", [False, True])
@pytest.mark.parametrize("held_later", [False, True])
async def test_replay_keeps_chained_record_edits_between_fold_and_shed(
    shed_first: bool, durable_shed: bool, held_later: bool
) -> None:
    from maf_compaction._shed import unique_shed_groups

    messages = _history(2)
    narration = Message(
        "assistant",
        ["Intervening narration."],
        message_id="narration" if durable_shed else "msg_20",
    )
    messages[4:4] = [
        Message("user", ["First facts. " * 100], message_id="middle0"),
        narration,
        Message("user", ["Second facts. " * 100], message_id="middle1"),
    ]
    original = copy.deepcopy(messages)
    text = " ".join(sorted(_sent_values(messages)))
    answers = iter([text + ". Extra explanation. " * 12, text])
    summarizer = _Summarizer(merge=lambda _: next(answers))
    strategy = _strategy(summarizer, budget=100_000, cap=None)
    _size(messages)
    for index in range(2):
        group = next(
            group
            for group in group_messages(messages)
            if messages[group["start_index"]].message_id == f"middle{index}"
        )
        strategy.user_turns._replace(
            messages,
            [group],
            f"Summary {index}. " * 40,
            summary_id=f"summary_{index}",
        )
    assert strategy._reshape_records(messages)

    async def shed_or_fold(shed: bool) -> None:
        if not shed:
            assert await strategy.user_turns.fold_if_smaller(messages) == "folded"
        else:
            if durable_shed:
                strategy._shed_ids.add("narration")
            else:
                key = next(
                    key
                    for key, members in unique_shed_groups(messages)
                    if any(message is narration for message in members)
                )
                strategy._shed_groups.add(key)
            assert strategy._keep_shedding(messages)

    for shed in (shed_first, not shed_first):
        await shed_or_fold(shed)
        groups = consolidatable_record_groups(messages)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    for history in (messages, original):
        _retain_later_reasoning(history)
    if held_later:
        _hold_later_user_edits(original, messages)
    cold_summarizer = _Summarizer()
    cold = _strategy(cold_summarizer, budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert await cold(original)
    assert _sent(original) == _sent(messages)
    assert not cold_summarizer.requests
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


def test_a_partial_reshape_cannot_edit_reasoning_left_out_of_its_plan() -> None:
    messages = _reasoned(2)
    groups = reshapeable_record_groups(messages)
    assert len(groups) == 2
    before = _sent(messages)
    strategy = _strategy(_Summarizer(), budget=100_000)
    with pytest.raises(ValueError, match="retained reasoning prefix"):
        strategy.tool_results.reshape_records(messages, groups[:1])
    assert _sent(messages) == before


def test_reasoned_records_do_not_rescan_history_once_per_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan = toolsummary_module._consolidatable_record_groups
    scans = 0

    def counted(messages: list[Message], *, replay: bool = False):
        nonlocal scans
        scans += 1
        return scan(messages, replay=replay)

    monkeypatch.setattr(toolsummary_module, "_consolidatable_record_groups", counted)
    messages = _reasoned(80)
    strategy = _strategy(_Summarizer(), budget=100_000)
    assert strategy._reshape_records(messages)
    assert strategy.records_reshaped == 80
    assert scans <= 4


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


def _different_sources(messages: list[Message]) -> list[Message]:
    other = copy.deepcopy(messages)
    for message in other:
        if message.message_id:
            message.message_id = f"other-{message.message_id}"
        for content in message.contents:
            if content.call_id:
                content.call_id = f"other-{content.call_id}"
    annotate_message_groups(other)
    return other


def _retain_later_reasoning(messages: list[Message]) -> None:
    later = _tool_group(20)
    later[0].contents.insert(0, Content.from_text_reasoning(text="Retain this prefix."))
    messages[-1:-1] = later
    annotate_message_groups(messages)


@pytest.mark.parametrize("restore", [False, True])
def test_reshape_replay_does_not_authorize_duplicate_bodies(restore: bool) -> None:
    messages = _reasoned(1)
    duplicate = _different_sources(messages)
    strategy = _strategy(_Summarizer(), budget=100_000)
    assert strategy._reshape_records(messages)
    if restore:
        cold = _strategy(_Summarizer(), budget=100_000)
        cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
        strategy = cold
    _retain_later_reasoning(duplicate)
    before = _sent(duplicate)
    assert not strategy._reshape_records(duplicate, replay=True)
    assert _sent(duplicate) == before


@pytest.mark.parametrize("shape", ["preceding", "co-located"])
def test_reshape_replay_requires_the_complete_accepted_batch(shape: str) -> None:
    messages = _reasoned(2, shape)
    original = copy.deepcopy(messages)
    strategy = _strategy(_Summarizer(), budget=100_000)
    assert strategy._reshape_records(messages)
    next(message for message in original if message.message_id == "rec1_res").contents[
        0
    ].additional_properties["classification"] = "changed"
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    before = _sent(original)
    assert not cold._reshape_records(original, replay=True)
    assert _sent(original) == before


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["merge", "rewrite"])
async def test_consolidation_replay_does_not_authorize_duplicate_bodies(operation: str) -> None:
    messages = _reasoned(1)
    if operation == "merge":
        messages[-1:-1] = _record_pair(1)
        annotate_message_groups(messages)
    duplicate = _different_sources(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000)
    groups = consolidatable_record_groups(messages)
    assert (
        await strategy._consolidate(
            messages,
            groups,
            prompt=DEFAULT_RECORD_MERGE_PROMPT,
            transcript=_transcript(messages, groups),
        )
        == "accepted"
    )
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    _retain_later_reasoning(duplicate)
    before = _sent(duplicate)
    assert not cold._keep_decisions(duplicate)
    assert _sent(duplicate) == before


@pytest.mark.anyio
async def test_rewritten_record_replay_preserves_consolidated_source_lineage() -> None:
    messages = _history(2)
    duplicate = _different_sources(messages)
    text = " ".join(sorted(_sent_values(messages)))
    intermediate = text + ". Additional record explanation. " * 12
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000)
    for history in (messages, duplicate):
        strategy.tool_results.consolidate_records(
            history, consolidatable_record_groups(history), intermediate
        )
    groups = consolidatable_record_groups(messages)
    assert (
        await strategy._consolidate(
            messages,
            groups,
            prompt=DEFAULT_RECORD_MERGE_PROMPT,
            transcript=_transcript(messages, groups),
        )
        == "accepted"
    )
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    _retain_later_reasoning(duplicate)
    before = _sent(duplicate)
    assert not cold._keep_decisions(duplicate)
    assert _sent(duplicate) == before


@pytest.mark.parametrize("change", ["reasoning", "interleaved", "missing"])
def test_reshape_batch_replay_requires_unchanged_source_window(change: str) -> None:
    messages = _reasoned(2)
    original = copy.deepcopy(messages)
    strategy = _strategy(_Summarizer(), budget=100_000)
    assert strategy._reshape_records(messages)
    if change == "reasoning":
        original[2].contents[0].protected_data = "changed-reasoning"
    elif change == "interleaved":
        original.insert(5, Message("assistant", ["A different intervening answer."]))
    else:
        original[:] = [
            m for m in original if m.message_id not in {"think1", "rec1_call", "rec1_res"}
        ]
    _retain_later_reasoning(original)
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    before = _sent(original)
    assert not cold._reshape_records(original, replay=True)
    assert _sent(original) == before


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "rewrite"])
@pytest.mark.parametrize("certificate", ["missing", "v1"])
async def test_legacy_record_decisions_do_not_bypass_retained_reasoning(
    operation: str, certificate: str
) -> None:
    messages = _reasoned(1)
    original = copy.deepcopy(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000)
    if operation == "reshape":
        assert strategy._reshape_records(messages)
    else:
        groups = consolidatable_record_groups(messages)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    state = json.loads(json.dumps(strategy.session_state()))
    if certificate == "missing":
        state.pop("record_order", None)
    else:
        for entry in state["record_order"]:
            entry["sources"] = entry["sources"].removeprefix("v2:")
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(state)
    _retain_later_reasoning(original)
    before = _sent(original)
    assert not cold._reshape_records(original, replay=True)
    assert not cold._keep_decisions(original)
    assert _sent(original) == before


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "merge"])
@pytest.mark.parametrize("restore_exclusions", [False, True])
@pytest.mark.parametrize("placement", ["between", "after"])
async def test_replay_checks_original_reasoning_exclusion_state(
    operation: str, restore_exclusions: bool, placement: str
) -> None:
    messages = _history(2)
    between = _tool_group(19)
    between[0].contents.insert(0, Content.from_text_reasoning(text="Protect this prefix."))
    insertion = 4 if placement == "between" else len(messages) - 1
    messages[insertion:insertion] = between
    original = copy.deepcopy(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000)
    if restore_exclusions:
        strategy._shed_ids.update(message.message_id for message in between if message.message_id)
        assert strategy._keep_shedding(messages)
    else:
        for message in between:
            message.additional_properties[EXCLUDED_KEY] = True
    annotate_message_groups(messages)
    if operation == "reshape":
        assert strategy._reshape_records(messages)
    else:
        groups = consolidatable_record_groups(messages)
        assert len(groups) == 2
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    _retain_later_reasoning(original)
    if restore_exclusions:
        _retain_later_reasoning(messages)
        if operation == "reshape":
            assert await cold(original)
        else:
            assert cold._keep_decisions(original)
        assert _sent(original) == _sent(messages)
    else:
        before = _sent(original)
        assert not cold._reshape_records(original, replay=True)
        assert not cold._keep_decisions(original)
        assert _sent(original) == before


def test_replay_miss_serializes_each_message_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from maf_compaction._record_replay import replay_match, source_key
    from maf_compaction._toolsummary import build_record_message

    messages = [
        build_record_message(f"Record {index}: " + "detail " * 2_000) for index in range(80)
    ]
    for index, message in enumerate(messages):
        message.message_id = f"record-{index}"
    annotate_message_groups(messages)
    groups = consolidatable_record_groups(messages)
    accepted = source_key(messages, groups[:40])
    assert accepted is not None
    messages[0].contents[0].text += " changed"
    serializations = 0
    original = Content.to_dict

    def counted(self, *args, **kwargs):
        nonlocal serializations
        serializations += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Content, "to_dict", counted)
    assert replay_match(messages, groups, {(accepted, 40)}) is None
    assert serializations <= len(messages)


@pytest.mark.parametrize("shedding", [False, True])
@pytest.mark.parametrize("user_summary", [False, True])
def test_ordered_replay_misses_share_message_fingerprints(
    monkeypatch: pytest.MonkeyPatch, shedding: bool, user_summary: bool
) -> None:
    from maf_compaction._shed import unique_shed_groups
    from maf_compaction._toolsummary import build_record_message

    messages = [build_record_message(f"Record {index}: " + "detail " * 100) for index in range(40)]
    for index, message in enumerate(messages):
        message.message_id = f"record-{index}"
    annotate_message_groups(messages)
    strategy = _strategy(_Summarizer(), budget=100_000)
    if user_summary:
        messages.append(Message("user", ["Facts. " * 1000], message_id="source-user"))
        annotate_message_groups(messages)
        group = next(
            g
            for g in group_messages(messages)
            if messages[g["start_index"]].message_id == "source-user"
        )
        strategy.user_turns._replace(messages, [group], "Facts.", summary_id="held-summary")
    if shedding:
        narration = Message("assistant", ["Narration."], message_id="msg_50")
        messages.append(narration)
        annotate_message_groups(messages)
        strategy._shed_groups.add(
            next(
                key
                for key, members in unique_shed_groups(messages)
                if any(message is narration for message in members)
            )
        )
        assert strategy._keep_shedding(messages)
    for group in consolidatable_record_groups(messages):
        edit = strategy._record_edit(messages, [group], "replacement")
        assert edit is not None
        strategy._record_order.append(edit)
    for message in messages[:40]:
        message.contents[0].text += " changed"
    serializations = 0
    restorations = 0
    original = Content.to_dict
    replay_summaries = type(strategy.user_turns).replay_summaries

    def restore(self, *args, **kwargs):
        nonlocal restorations
        restorations += 1
        return replay_summaries(self, *args, **kwargs)

    def counted(self, *args, **kwargs):
        nonlocal serializations
        serializations += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Content, "to_dict", counted)
    monkeypatch.setattr(type(strategy.user_turns), "replay_summaries", restore)
    assert not strategy._replay_record_edits(messages)
    assert restorations <= 1
    assert serializations <= len(messages) * (2 if shedding else 1)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "merge", "rewrite"])
@pytest.mark.parametrize("kind", ["text", "uri", "data", "hosted_file"])
async def test_user_media_only_turn_completes_recall_reasoning(operation: str, kind: str) -> None:
    messages = _reasoned(1)
    if operation == "merge":
        messages[-1:-1] = _record_pair(1)
    contents = {
        "text": Content.from_text("Next request."),
        "uri": Content.from_uri("https://example.org/image.png", media_type="image/png"),
        "data": Content.from_data(b"image", media_type="image/png"),
        "hosted_file": Content.from_hosted_file("file-a", media_type="image/png"),
    }
    messages[-1].contents = [contents[kind]]
    annotate_message_groups(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000)
    if operation == "reshape":
        assert strategy._reshape_records(messages)
    else:
        groups = consolidatable_record_groups(messages)
        assert len(groups) == (2 if operation == "merge" else 1)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    assert not any(
        part.type == "text_reasoning"
        for m in project_included_messages(messages)
        for part in m.contents
    )
    assert messages[-1].contents == [contents[kind]]


@pytest.mark.anyio
async def test_consolidated_lineage_does_not_depend_on_later_excluded_reasoning() -> None:
    messages = _history(2)
    original = copy.deepcopy(messages)
    text = " ".join(sorted(_sent_values(messages)))
    answers = iter([text + ". Additional explanation. " * 12, text])
    strategy = _strategy(_Summarizer(merge=lambda _: next(answers)), budget=100_000)
    for _ in range(2):
        groups = consolidatable_record_groups(messages)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    excluded = Message(
        "assistant",
        [Content.from_text_reasoning(text="Later excluded thought.")],
        message_id="later-excluded",
    )
    excluded.additional_properties[EXCLUDED_KEY] = True
    retained = _tool_group(20)
    retained[0].contents.insert(0, Content.from_text_reasoning(text="Used the compacted prefix."))
    for history in (messages, original):
        history.extend(copy.deepcopy([excluded, *retained]))
        annotate_message_groups(history)
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert cold._keep_decisions(original)
    assert _sent(original) == _sent(messages)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "merge"])
@pytest.mark.parametrize("message_id", [None, "msg_5"])
async def test_anonymous_excluded_reasoning_cannot_gain_replay_authority(
    operation: str, message_id: str | None
) -> None:
    messages = _history(2)
    reasoning = Content.from_text_reasoning(text="Keep the original prefix.")
    reasoning.additional_properties["signature"] = "original-signature"
    later = Message("assistant", [reasoning], message_id=message_id)
    messages.append(later)
    original = copy.deepcopy(messages)
    later.additional_properties[EXCLUDED_KEY] = True
    annotate_message_groups(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000)
    if operation == "reshape":
        assert strategy._reshape_records(messages)
    else:
        groups = consolidatable_record_groups(messages)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    original[-1].contents[0].additional_properties["classification"] = "changed"
    cold = _strategy(_Summarizer(), budget=100_000)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    before = _sent(original)
    assert not cold._reshape_records(original, replay=True)
    assert not cold._keep_decisions(original)
    assert _sent(original) == before


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["reshape", "merge"])
@pytest.mark.parametrize("summary_first", [False, True])
@pytest.mark.parametrize("user_edit", ["summary", "fold"])
async def test_record_replay_restores_interleaved_user_summary(
    operation: str, summary_first: bool, user_edit: str
) -> None:
    messages = _history(2)
    messages.insert(4, Message("user", ["Additional facts. " * 1_000], message_id="middle"))
    if user_edit == "fold":
        messages.insert(5, Message("user", ["More facts. " * 1_000], message_id="middle2"))
    original = copy.deepcopy(messages)
    text = " ".join(sorted(_sent_values(messages)))
    strategy = _strategy(_Summarizer(merge=lambda _: text), budget=100_000, cap=None)

    async def summarize() -> None:
        if user_edit == "fold":
            _size(messages)
            for index, identity in enumerate(("middle", "middle2")):
                group = next(
                    group
                    for group in group_messages(messages)
                    if messages[group["start_index"]].message_id == identity
                )
                strategy.user_turns._replace(
                    messages,
                    [group],
                    f"Summary {index}. " * 100,
                    summary_id=f"summary_{index}",
                )
            assert await strategy.user_turns.fold_if_smaller(messages) == "folded"
            return
        assert await strategy.user_turns.compact_against(
            messages,
            prompt_tokens=_size(messages),
            trigger_tokens=1,
        )

    if summary_first:
        await summarize()
    if operation == "reshape":
        assert strategy._reshape_records(messages)
    else:
        groups = consolidatable_record_groups(messages)
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    if not summary_first:
        await summarize()
    for history in (messages, original):
        _retain_later_reasoning(history)
    cold = _strategy(_Summarizer(), budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    assert await cold(original)
    assert _sent(original) == _sent(messages)
