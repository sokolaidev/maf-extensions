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
    set_excluded,
)
from test_anchored import TOKENIZER, _conversation

from maf_compaction import _anchored as anchored_module
from maf_compaction._anchored import EXCLUDE_REASON, MARKER_ID_PREFIX, AnchoredCompactionStrategy
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
