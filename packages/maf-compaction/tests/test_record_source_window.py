"""A batch's source key does not depend on a pass's own exclusions between its records."""

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
    included_token_count,
)
from test_background_record_merges import running, tool_reply
from test_record_budget import (
    TOKENIZER,
    _history,
    _record_pair,
    _sent,
    _size,
    _strategy,
    _Summarizer,
    _values,
)

from maf_compaction._anchored import EXCLUDE_REASON as SHED_EXCLUDE_REASON
from maf_compaction._artifacts import (
    MARKER_ID_PREFIX,
    SHED_NOTES,
    TOOL_CALL,
    USER_FOLD_EXCLUDE_REASON,
    USER_SUMMARY_MARKER,
    is_marker,
    is_user_summary,
)
from maf_compaction._background_records import BackgroundRecords
from maf_compaction._composed import (
    DEFAULT_RECORD_MERGE_PROMPT,
    _RecordMergeWork,
    _transcript,
    harder_record_prompt,
)
from maf_compaction._media import annotate_token_counts
from maf_compaction._record_replay import (
    RECORD_ANCHOR_EXCLUDE_REASON,
    _source_key,
    replay_match,
    source_key,
)
from maf_compaction._toolsummary import consolidatable_record_groups

pytestmark = pytest.mark.anyio

RECORDS = 4
#: Someone other than the compaction pass holds the message.
FOREIGN_REASON = "external-owner"


def _interleaved() -> list[Message]:
    """Each record after the tool round it covers, as a real tool loop writes them."""
    messages = _history(0)
    closing = messages.pop()
    for index in range(RECORDS):
        messages += [
            Message(
                "assistant",
                [Content.from_function_call(call_id=f"t{index}", name="lookup", arguments={})],
                message_id=f"t{index}_call",
            ),
            Message(
                "tool",
                [
                    Content.from_function_result(
                        call_id=f"t{index}", result=f"lookup {index} output"
                    )
                ],
                message_id=f"t{index}_res",
            ),
            *_record_pair(index),
        ]
    return [*messages, closing]


def _tool_rounds(messages: list[Message]) -> list[Message]:
    return [m for m in messages if str(m.message_id).startswith("t")]


def _exclude(messages: list[Message], reason: str) -> None:
    for message in _tool_rounds(messages):
        message.additional_properties[EXCLUDED_KEY] = True
        message.additional_properties[EXCLUDE_REASON_KEY] = reason


def _include(messages: list[Message]) -> None:
    for message in _tool_rounds(messages):
        message.additional_properties.pop(EXCLUDED_KEY, None)
        message.additional_properties.pop(EXCLUDE_REASON_KEY, None)


def _key(messages: list[Message]) -> str | None:
    return source_key(messages, consolidatable_record_groups(messages))


@pytest.mark.parametrize("reason", [RECORD_ANCHOR_EXCLUDE_REASON, SHED_EXCLUDE_REASON])
def test_a_pass_exclusion_between_the_records_leaves_the_key_as_it_was(reason: str) -> None:
    """Covered or shed, the tool rounds between the records give the key they give included."""
    included = _interleaved()
    excluded = copy.deepcopy(included)
    _exclude(excluded, reason)
    assert _key(included) is not None
    assert _key(excluded) == _key(included)


def test_with_nothing_excluded_the_key_is_unchanged() -> None:
    """A saved key from before the change still matches a history with nothing excluded."""
    from maf_compaction import _record_replay as replay

    messages = _interleaved()
    groups = consolidatable_record_groups(messages)
    old_payload = [
        [
            [g["start_index"] - groups[0]["start_index"], g["end_index"] - groups[0]["start_index"]]
            for g in groups
        ],
        [
            replay._message_key(messages[index])  # pyright: ignore[reportPrivateUsage]
            for index in range(groups[0]["start_index"], groups[-1]["end_index"] + 1)
        ],
    ]
    import hashlib

    old = hashlib.sha256(json.dumps(old_payload, sort_keys=True, default=str).encode()).hexdigest()
    assert replay.source_lineage(messages, groups) == old


@pytest.mark.parametrize(
    "change",
    ["foreign exclusion", "reasoning excluded", "record changed", "tool round changed"],
)
def test_anything_else_between_or_in_the_records_still_changes_the_key(change: str) -> None:
    before = _interleaved()
    after = copy.deepcopy(before)
    if change == "foreign exclusion":
        _exclude(after, FOREIGN_REASON)
    elif change == "reasoning excluded":
        index = next(i for i, m in enumerate(after) if m.message_id == "t1_res") + 1
        for messages in (before, after):
            messages.insert(
                index,
                Message(
                    "assistant", [Content.from_text_reasoning(text="thinking")], message_id="r1"
                ),
            )
        reasoning = next(m for m in after if m.message_id == "r1")
        reasoning.additional_properties[EXCLUDED_KEY] = True
        reasoning.additional_properties[EXCLUDE_REASON_KEY] = SHED_EXCLUDE_REASON
    elif change == "record changed":
        record = next(m for m in after if m.message_id == "rec1_res")
        record.contents[0].result += " changed"
    else:
        result = next(m for m in after if m.message_id == "t2_res")
        result.contents[0].result += " changed"
    assert _key(before) != _key(after)


async def test_a_background_answer_applies_after_the_pass_covers_the_tool_rounds():
    """The worker prepared the merge with the tool rounds included; the next pass has them
    excluded as covered, and applies the answer instead of discarding it."""
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _interleaved()
    await strategy(messages)
    # The pass the worker starts from has the tool rounds included.
    _include(messages)
    await work.start(tool_reply())
    await running(work)
    assert work._ready, "the premise: the worker prepared an answer"

    _exclude(messages, RECORD_ANCHOR_EXCLUDE_REASON)

    assert await work.apply_ready(messages)
    assert strategy.records_merged == 1
    await work.close()


@pytest.mark.parametrize(
    ("accepted_with", "replayed_on"),
    [
        (RECORD_ANCHOR_EXCLUDE_REASON, None),
        (None, RECORD_ANCHOR_EXCLUDE_REASON),
        (SHED_EXCLUDE_REASON, None),
    ],
    ids=[
        "accepted covered, replayed included",
        "accepted included, replayed covered",
        "accepted shed, replayed included",
    ],
)
@pytest.mark.parametrize("operation", ["merge", "rewrite"])
async def test_a_kept_merge_is_put_back_whatever_the_pass_excluded_between_its_records(
    accepted_with: str | None, replayed_on: str | None, operation: str
) -> None:
    """The edit's key was taken under one pass's exclusions and is matched under another's: the
    merge or rewrite is put back, not asked again. On a long conversation a rewrite of the same
    five records was asked eleven times in one turn this way."""
    text = " ".join(sorted(v for i in range(RECORDS) for v in _values(i)))
    summarizer = _Summarizer(merge=lambda _: text, rewrite=lambda _: text)
    prompt = DEFAULT_RECORD_MERGE_PROMPT if operation == "merge" else harder_record_prompt(1)
    strategy = _strategy(summarizer, budget=100_000, cap=None)
    messages = _interleaved()
    if accepted_with is not None:
        _exclude(messages, accepted_with)
    groups = consolidatable_record_groups(messages)
    assert (
        await strategy._consolidate(
            messages,
            groups,
            prompt=prompt,
            transcript=_transcript(messages, groups),
        )
        == "accepted"
    )
    merged = [m for m in _sent(messages) if text in json.dumps(m[1])]
    assert merged, "the premise: the merged record is sent"

    cold = _strategy(_Summarizer(), budget=100_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    later = _interleaved()
    if replayed_on is not None:
        _exclude(later, replayed_on)

    assert cold._replay_record_edits(later)
    assert any(text in json.dumps(m[1]) for m in _sent(later)), "the merged record is back"


def test_a_fingerprint_is_byte_for_byte_what_it_was() -> None:
    """Contents serialized apart and spliced in give the bytes the whole list gave, so every key
    saved before the change still matches."""
    import hashlib

    from maf_compaction._preserve import STANDS_IN_KEY
    from maf_compaction._record_replay import LINEAGE_KEY, MessageKeyCache, _message_key
    from maf_compaction._shed import durable_message_id

    def before(message: Message) -> str:
        properties = message.additional_properties
        payload = [
            message.role,
            message.author_name,
            message.message_id
            if durable_message_id(message.message_id)
            and not properties.get(STANDS_IN_KEY)
            and not properties.get(LINEAGE_KEY)
            else None,
            properties.get(STANDS_IN_KEY),
            properties.get(LINEAGE_KEY),
            bool(properties.get(EXCLUDED_KEY, False)),
            [content.to_dict(exclude={"raw_representation"}) for content in message.contents],
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    covered = _interleaved()
    _exclude(covered, RECORD_ANCHOR_EXCLUDE_REASON)
    cache = MessageKeyCache()
    for message in [*_interleaved(), *covered]:
        assert _message_key(message) == before(message)  # pyright: ignore[reportPrivateUsage]
        assert cache.key(message) == before(message)


def _with_later_reasoning(messages: list[Message]) -> list[Message]:
    """Retained reasoning after every record: the guard decides which records are eligible."""
    closing = messages.pop()
    thinking = Message(
        "assistant",
        [Content.from_text_reasoning(text="planning the next step"), Content.from_text("next")],
        message_id="later_reasoning",
    )
    return [*messages, thinking, closing]


async def test_an_answer_captured_over_budget_applies_in_a_pass_that_fits():
    """The snapshot lifted the reasoning guard; a pass that fits applies its answer with the
    records the snapshot saw, instead of finding none and discarding it (#2108)."""
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None, policy="shed")
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _with_later_reasoning(_interleaved())
    await strategy(messages)
    # The snapshot and the worker's search are over budget, as on a long conversation.
    work._budget = 1
    await work.start(tool_reply())
    await running(work)
    assert work._ready, "the premise: the worker prepared an answer"
    assert (work._ready[0].captured_guarded, work._ready[0].searched_guarded) == (False, False)
    # The pass that applies it fits, so its own guard is on.
    assert await work.apply_ready(messages)
    assert strategy.records_merged == 1
    await work.close()


def _shed_with_notes(strategy, messages: list[Message]) -> None:
    """Shed the tool rounds between the records the way a pass does, leaving its notes."""
    ids = {str(m.message_id) for m in _tool_rounds(messages)}
    assert strategy.tool_results.fallback.shed_again(messages, ids)
    assert any(is_marker(m) for m in messages), "the premise: the shed left notes"


def _without_notes(messages: list[Message]) -> list[Message]:
    return [m for m in messages if not is_marker(m)]


async def test_a_background_answer_applies_when_the_next_pass_lacks_the_shed_notes():
    """The worker snapshots the list after the pass's shed, notes included; the next pass's copy
    has the same tool rounds shed but no notes until its own shed, and applies the answer (#2108)."""
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _interleaved()
    _shed_with_notes(strategy, messages)
    await strategy(messages)
    assert any(is_marker(m) for m in messages), "the premise: the pass kept the notes"
    work.remember(messages, strategy._budget)
    await work.start(tool_reply())
    await running(work)
    assert work._ready, "the premise: the worker prepared an answer"

    applying = _without_notes(messages)

    assert await work.apply_ready(applying)
    assert strategy.records_merged == 1
    await work.close()


def test_the_shed_notes_between_the_records_leave_the_key_as_it_was() -> None:
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    noted = _interleaved()
    _shed_with_notes(strategy, noted)
    plain = _without_notes(copy.deepcopy(noted))

    assert _key(noted) == _key(plain)


def test_an_edit_kept_under_the_key_before_notes_were_skipped_still_replays() -> None:
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    messages = _interleaved()
    _shed_with_notes(strategy, messages)
    groups = consolidatable_record_groups(messages)
    legacy = _source_key(messages, groups, {}, skip_notes=False)
    assert legacy is not None and legacy != _source_key(messages, groups, {})

    found = replay_match(messages, groups, [(f"v2:{legacy}:", len(groups))])

    assert found is not None and found[0] == groups


def _note(index: int) -> Message:
    """A shed's note, standing on its own between two records."""
    return Message(
        "assistant",
        [SHED_NOTES[TOOL_CALL]],
        message_id=f"{MARKER_ID_PREFIX}g{index}_1",
        additional_properties={
            GROUP_ANNOTATION_KEY: {
                SUMMARY_OF_MESSAGE_IDS_KEY: [f"shed{index}"],
                SUMMARY_OF_GROUP_IDS_KEY: [f"g{index}"],
            }
        },
    )


def test_a_gap_of_notes_only_still_counts_in_the_snapshot() -> None:
    """No gap is left for notes alone, but they are sent, so their tokens stay in the snapshot's
    count; a snapshot short of them could read an over-budget history as under it."""
    messages = _history(RECORDS)
    messages.insert(4, _note(0))
    assert is_marker(messages[4]), "the premise: a note between records 0 and 1"
    annotate_message_groups(messages)
    annotate_token_counts(messages, tokenizer=TOKENIZER)

    plain = [m for m in copy.deepcopy(messages) if not is_marker(m)]
    annotate_message_groups(plain)

    records = BackgroundRecords.capture(messages)

    assert included_token_count(records.messages) == included_token_count(messages)
    gaps = [str(m.message_id) for m in BackgroundRecords.capture(plain).messages]
    assert [str(m.message_id) for m in records.messages] == gaps, "the note adds no gap"


async def test_two_dependent_edits_kept_under_the_key_with_notes_both_replay(monkeypatch) -> None:
    """Two merges accepted before notes were skipped, the second taking in the first's record:
    after a restore both are put back, the second naming the first by its lineage then."""
    from maf_compaction import _record_replay as replay

    current = replay._source_key
    monkeypatch.setattr(
        replay, "_source_key", lambda *args, **kwargs: current(*args, **kwargs, skip_notes=False)
    )
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    messages = _interleaved()
    _shed_with_notes(strategy, messages)
    for _ in range(2):
        groups = consolidatable_record_groups(messages)[:2]
        outcome = await strategy._consolidate(
            messages,
            groups,
            prompt=DEFAULT_RECORD_MERGE_PROMPT,
            transcript=_transcript(messages, groups),
        )
        assert outcome == "accepted"
    merged = [str(m[1]) for m in _sent(messages)]
    state = json.loads(json.dumps(strategy.session_state()))
    monkeypatch.setattr(replay, "_source_key", current)

    cold = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    cold.restore_session_state(state)
    later = _interleaved()
    _shed_with_notes(cold, later)

    assert cold._replay_record_edits(later)
    assert [str(m[1]) for m in _sent(later)] == merged


def _user_turns_between_records() -> list[Message]:
    """Two records with a long user turn and a reply between them, as a long conversation has."""
    messages = _history(2)
    messages[4:4] = [
        Message("user", ["First facts. " * 100], message_id="middle0"),
        Message("assistant", ["Intervening narration."], message_id="narration"),
    ]
    return messages


async def test_a_background_answer_applies_after_the_user_phase_folds_a_turn_between_its_records():
    """The worker snapshots the list before the next pass's user phase, which summarises a turn
    between the records before that pass applies ready answers. The answer is applied (#2108)."""
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    work = _RecordMergeWork()
    work.bind(strategy, None)
    messages = _user_turns_between_records()
    _size(messages)
    work.remember(messages, strategy._budget)
    await work.start(tool_reply())
    await running(work)
    assert work._ready, "the premise: the worker prepared an answer"

    group = next(
        g for g in group_messages(messages) if messages[g["start_index"]].message_id == "middle0"
    )
    strategy.user_turns._replace(messages, [group], "Summary. " * 40, summary_id="summary_0")
    assert any(is_user_summary(m) for m in messages), "the premise: the turn is summarised"

    assert await work.apply_ready(messages)
    assert strategy.records_merged == 1
    await work.close()


class _MessageCounter:
    """A tokenizer that counts whole messages, so each snapshot counts its history again."""

    def count_message_tokens(self, message: Message) -> int:
        return 4 + sum(len(json.dumps(c.to_dict(), default=str)) for c in message.contents) // 4

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


@pytest.mark.parametrize("between", ["a user summary", "a shed's note"])
async def test_a_recounting_snapshot_still_reads_what_stands_between_the_records(between: str):
    """A tokenizer that counts whole messages makes the snapshot count its history again, and
    the annotation it drops carried the links that make a summary or a note one. The snapshot
    still leaves it out of its gaps, as the applying pass does, and the answer applies."""
    strategy = _strategy(
        _Summarizer(), budget=100_000, share=0.001, cap=None, tokenizer=_MessageCounter()
    )
    work = _RecordMergeWork()
    work.bind(strategy, None)
    if between == "a user summary":
        messages = _user_turns_between_records()
        _size(messages)
        group = next(
            g
            for g in group_messages(messages)
            if messages[g["start_index"]].message_id == "middle0"
        )
        strategy.user_turns._replace(messages, [group], "Summary. " * 40, summary_id="summary_0")
        assert any(is_user_summary(m) for m in messages)
    else:
        messages = _interleaved()
        _shed_with_notes(strategy, messages)
    work.remember(messages, strategy._budget)
    await work.start(tool_reply())
    await running(work)
    assert work._ready, "the premise: the worker prepared an answer"

    assert await work.apply_ready(messages)
    assert strategy.records_merged == 1
    await work.close()


def test_a_summary_carrying_reasoning_still_counts_in_the_gaps() -> None:
    """Reasoning counts where it stands, whatever message carries it: an excluded summary holding
    reasoning is not left out of the gaps, so removing it changes the snapshot."""
    messages = _history(2)
    summary = Message(
        "user",
        [
            Content.from_text(f"{USER_SUMMARY_MARKER} Earlier turns."),
            Content.from_text_reasoning(text="Weighing the earlier turns."),
        ],
        message_id="summary_r",
        additional_properties={
            GROUP_ANNOTATION_KEY: {
                SUMMARY_OF_MESSAGE_IDS_KEY: ["gone"],
                SUMMARY_OF_GROUP_IDS_KEY: ["group_gone"],
            },
            # A summary a fold superseded: excluded, and only reasoning keeps it in the key.
            EXCLUDED_KEY: True,
            EXCLUDE_REASON_KEY: USER_FOLD_EXCLUDE_REASON,
        },
    )
    assert is_user_summary(summary), "the premise: it reads as a summary"
    with_it = [*messages[:4], summary, *messages[4:]]
    _size(with_it)
    _size(messages)

    gaps = [str(m.message_id) for m in BackgroundRecords.capture(with_it).messages]
    without = [str(m.message_id) for m in BackgroundRecords.capture(messages).messages]

    assert gaps != without
