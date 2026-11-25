"""Bound note insertion work and compare its output with sequential insertion."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from agent_framework import Message
from agent_framework._compaction import (
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_message_groups,
    annotate_token_counts,
    group_messages,
    included_token_count,
    project_included_messages,
    set_excluded,
)
from test_anchored import TOKENIZER, _conversation, _tool_turn
from test_completed_turns import _strategy as composed_strategy

from maf_compaction import _anchored as anchored_module
from maf_compaction._anchored import EXCLUDE_REASON, MARKER_ID_PREFIX, AnchoredCompactionStrategy
from maf_compaction._artifacts import SHED_NOTES, TOOL_CALL, is_compaction_echo, is_marker
from maf_compaction._preserve import set_preserved


class _SequentialNotes(AnchoredCompactionStrategy):
    """Reference the per-group scan and insertion contract independently of the batch."""

    def _insert_notes(
        self, messages: list[Message], notes: list[tuple[dict[str, Any], str]]
    ) -> None:
        for group, note in notes:
            if "kind" in group:
                note = self._note_for(messages, group)
            group_id = group["group_id"]
            if any(
                anchored_module._is_marker(message)
                and group_id
                in message.additional_properties.get(GROUP_ANNOTATION_KEY, {}).get(
                    SUMMARY_OF_GROUP_IDS_KEY, []
                )
                for message in messages
            ):
                continue
            taken = {message.message_id for message in messages if message.message_id}
            marker_id = f"{MARKER_ID_PREFIX}{group_id}"
            suffix = 0
            while marker_id in taken:
                suffix += 1
                marker_id = f"{MARKER_ID_PREFIX}{group_id}_{suffix}"
            members = messages[group["start_index"] : group["end_index"] + 1]
            messages.insert(
                group["start_index"],
                Message(
                    role="assistant",
                    contents=[note],
                    message_id=marker_id,
                    additional_properties={
                        GROUP_ANNOTATION_KEY: {
                            SUMMARY_OF_MESSAGE_IDS_KEY: [
                                m.message_id for m in members if m.message_id
                            ],
                            SUMMARY_OF_GROUP_IDS_KEY: [group_id],
                        }
                    },
                ),
            )


def _strategy(
    cls: type[AnchoredCompactionStrategy] = AnchoredCompactionStrategy,
) -> AnchoredCompactionStrategy:
    return cls(
        max_input_tokens=1_000_000,
        tokenizer=TOKENIZER,
        keep_head_groups=1,
        keep_tail_groups=1,
    )


def _narration(turns: int) -> list[Message]:
    messages = [Message(role="user", contents=["Start."], message_id="start")]
    for index in range(turns):
        messages.extend(
            [
                Message(
                    role="assistant", contents=[f"Narration {index}. " * 80], message_id=f"a{index}"
                ),
                Message(role="user", contents=[f"Question {index}."], message_id=f"u{index}"),
            ]
        )
    return messages


@pytest.mark.parametrize("entry", ["shed", "shed_again"])
def test_note_marker_checks_grow_linearly(monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    original = anchored_module._is_marker
    counts: list[int] = []
    for turns in (100, 200):
        messages = _narration(turns)
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=TOKENIZER)
        checks = 0

        def counted(message: Message) -> bool:
            nonlocal checks
            checks += 1
            return original(message)

        monkeypatch.setattr(anchored_module, "_is_marker", counted)
        if entry == "shed":
            assert _strategy()._shed(messages, "assistant_text", ceiling=0)
        else:
            assert _strategy().shed_again(messages, {f"a{i}" for i in range(turns - 10)})
        assert sum(original(m) for m in messages) > turns // 2
        assert checks < 8 * (2 * turns + 1)
        counts.append(checks)
    assert counts[1] < 2.2 * counts[0]


@pytest.mark.parametrize("entry", ["shed", "shed_again"])
@pytest.mark.parametrize("kind", ["tool_call", "assistant_text"])
@pytest.mark.parametrize("already_excluded", [False, True])
def test_batched_shedding_matches_sequential_histories(
    entry: str, kind: str, already_excluded: bool
) -> None:
    messages = _conversation(tool_turns=12) if kind == "tool_call" else _narration(12)
    annotate_message_groups(messages)
    groups = [group for group in group_messages(messages) if group["kind"] == kind]
    target = groups[4]
    messages[0].message_id = f"{MARKER_ID_PREFIX}{target['group_id']}"
    messages.append(
        Message(
            role="developer",
            contents=["Keep the format."],
            message_id=f"{messages[0].message_id}_1",
        )
    )
    for group in groups[:2]:
        for message in messages[group["start_index"] : group["end_index"] + 1]:
            set_preserved(message, preserved=True)
    shed_ids = {
        message.message_id
        for group in groups[2:]
        for message in messages[group["start_index"] : group["end_index"] + 1]
        if message.message_id
    }
    if already_excluded:
        for message in messages:
            if message.message_id in shed_ids:
                set_excluded(message, excluded=True, reason=EXCLUDE_REASON)
    # A retained note can summarize several groups and need not use their preferred ids.
    messages.append(
        Message(
            role="assistant",
            contents=[anchored_module._NOTES[kind]],
            message_id=f"{MARKER_ID_PREFIX}retained",
            additional_properties={
                GROUP_ANNOTATION_KEY: {
                    SUMMARY_OF_MESSAGE_IDS_KEY: list(shed_ids),
                    SUMMARY_OF_GROUP_IDS_KEY: [groups[2]["group_id"], groups[3]["group_id"]],
                }
            },
        )
    )
    expected = copy.deepcopy(messages)
    strategy, sequential = _strategy(), _strategy(_SequentialNotes)
    for _ in range(2):
        annotate_message_groups(messages)
        annotate_message_groups(expected)
        annotate_token_counts(messages, tokenizer=TOKENIZER)
        annotate_token_counts(expected, tokenizer=TOKENIZER)
        if entry == "shed":
            assert strategy._shed(messages, kind, ceiling=0) == sequential._shed(
                expected, kind, ceiling=0
            )
        else:
            assert strategy.shed_again(messages, shed_ids) == sequential.shed_again(
                expected, shed_ids
            )
        assert [m.to_dict() for m in messages] == [m.to_dict() for m in expected]


def test_batch_reserves_suffixes_in_reverse_order_and_keeps_original_objects() -> None:
    messages = [
        Message(role="user", contents=["An occupied id."], message_id=f"{MARKER_ID_PREFIX}g"),
        Message(role="assistant", contents=["First."], message_id="a"),
        Message(role="assistant", contents=["Second."], message_id="b"),
        Message(role="assistant", contents=["Third."], message_id="c"),
    ]
    originals = list(messages)
    notes = [
        (
            {"group_id": group_id, "start_index": index, "end_index": index},
            anchored_module._NOTES["assistant_text"],
        )
        for index, group_id in [(3, "g_1"), (2, "g"), (1, "g")]
    ]
    expected = copy.deepcopy(messages)
    _strategy(_SequentialNotes)._insert_notes(expected, notes)
    _strategy()._insert_notes(messages, notes)

    assert [m.to_dict() for m in messages] == [m.to_dict() for m in expected]
    assert [m.message_id for m in messages] == [
        f"{MARKER_ID_PREFIX}g",
        "a",
        f"{MARKER_ID_PREFIX}g_2",
        "b",
        f"{MARKER_ID_PREFIX}g_1",
        "c",
    ]
    assert all(any(message is original for message in messages) for original in originals)


def test_batch_reads_the_history_a_bounded_number_of_times_and_never_inserts() -> None:
    class WatchedList(list[Message]):
        iterations = 0
        splices = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

        def insert(self, index: int, value: Message) -> None:
            pytest.fail("notes must be spliced together")

        def __setitem__(self, index: Any, value: Any) -> None:
            if isinstance(index, slice):
                self.splices += 1
            super().__setitem__(index, value)

    messages = WatchedList(_narration(100))
    annotate_message_groups(messages)
    notes = [
        (group, anchored_module._NOTES["assistant_text"])
        for group in reversed(group_messages(messages))
        if group["kind"] == "assistant_text"
    ]
    messages.iterations = 0
    _strategy()._insert_notes(messages, notes)
    assert messages.iterations <= 2
    assert messages.splices == 1
    assert len(messages) == 301


def test_new_notes_follow_visible_runs_and_extend_idempotently() -> None:
    first, second, third, fourth = (_tool_turn(i, 10)[:2] for i in range(4))
    barrier = Message("user", ["Continue."], message_id="keep")
    messages = [*first, *second, barrier, *third, *fourth]
    sources = list(messages)
    strategy = _strategy()
    source_ids = {m.message_id for m in [*first, *third, *fourth] if m.message_id}
    assert strategy.shed_again(messages, source_ids)
    visible = project_included_messages(messages)
    assert [m.text for m in visible if is_marker(m)] == [
        SHED_NOTES[TOOL_CALL],
        "[compacted: 2 earlier tool calls and their results]",
    ]
    assert visible[1:4] == [*second, barrier]
    source_ids.update(m.message_id for m in second if m.message_id)
    assert strategy.shed_again(messages, source_ids)
    left, kept, right = project_included_messages(messages)
    assert kept is barrier
    assert left.text == right.text == "[compacted: 2 earlier tool calls and their results]"
    for marker, members in [(left, [*first, *second]), (right, [*third, *fourth])]:
        links = marker.additional_properties[GROUP_ANNOTATION_KEY]
        assert links[SUMMARY_OF_GROUP_IDS_KEY] == [g["group_id"] for g in group_messages(members)]
        assert links[SUMMARY_OF_MESSAGE_IDS_KEY] == [m.message_id for m in members]
        assert not is_compaction_echo(marker)
    assert [id(m) for m in messages if not is_marker(m)] == [id(m) for m in sources]
    echo = Message("assistant", [left.text], message_id="anchored_imitation")
    assert is_compaction_echo(left.text) and is_compaction_echo(echo)
    assert not is_compaction_echo(f"Quoted {left.text}")
    messages.append(echo)
    assert strategy.shed_to(messages, ceiling=1_000_000)
    assert project_included_messages(messages) == [left, barrier, right]
    assert not strategy.shed_again(messages, source_ids)


@pytest.mark.anyio
async def test_composed_pass_merges_2176_stored_notes_before_budget_decisions() -> None:
    count = 2176
    messages = [m for i in range(count) for m in _tool_turn(i, 0)[:2]]
    groups = group_messages(messages)
    source_ids = [m.message_id for m in messages]
    for m in messages:
        set_excluded(m, excluded=True, reason=EXCLUDE_REASON)
    _strategy()._insert_notes(messages, [(g, SHED_NOTES[TOOL_CALL]) for g in reversed(groups)])
    annotate_token_counts(messages, tokenizer=TOKENIZER)
    before = included_token_count(messages)
    assert len(project_included_messages(messages)) == count
    strategy = composed_strategy(budget=1_000)
    assert before > strategy.max_input_tokens
    assert await strategy(messages)
    [note] = project_included_messages(messages)
    assert is_marker(note)
    assert note.text == "[compacted: 2176 earlier tool calls and their results]"
    links = note.additional_properties[GROUP_ANNOTATION_KEY]
    assert links[SUMMARY_OF_GROUP_IDS_KEY] == [g["group_id"] for g in groups]
    assert links[SUMMARY_OF_MESSAGE_IDS_KEY] == source_ids
    after = included_token_count(messages)
    assert 0 < after < 100
    annotate_token_counts(messages, tokenizer=TOKENIZER, force_retokenize=True)
    assert included_token_count(messages) == after
    assert not await strategy(messages)


@pytest.mark.parametrize("protected_note", [False, True])
def test_shed_to_keeps_an_included_barrier_between_runs(protected_note: bool) -> None:
    messages = [m for i in range(5) for m in _tool_turn(i, 500)[:2]]
    if protected_note:
        assert _strategy().shed_again(messages, {"a_call_2", "t_res_2"})
        barrier = next(m for m in messages if is_marker(m))
    else:
        barrier = Message("developer", ["Keep formatting."], message_id="keep")
        messages.insert(4, barrier)
    set_preserved(barrier, preserved=True)
    strategy = AnchoredCompactionStrategy(
        max_input_tokens=1, tokenizer=TOKENIZER, keep_head_groups=0, keep_tail_groups=0
    )
    assert strategy.shed_to(messages, ceiling=1)
    left, kept, right = project_included_messages(messages)
    assert kept is barrier
    assert left.text == "[compacted: 2 earlier tool calls and their results]"
    count = 2 if protected_note else 3
    assert right.text == f"[compacted: {count} earlier tool calls and their results]"
    assert not strategy.shed_to(messages, ceiling=1)
