"""A batch's source key does not depend on a pass's own exclusions between its records."""

from __future__ import annotations

import copy
import json

import pytest
from agent_framework import Content, Message
from agent_framework._compaction import EXCLUDE_REASON_KEY, EXCLUDED_KEY
from test_background_record_merges import running, tool_reply
from test_record_budget import _history, _record_pair, _sent, _strategy, _Summarizer, _values

from maf_compaction._anchored import EXCLUDE_REASON as SHED_EXCLUDE_REASON
from maf_compaction._composed import (
    DEFAULT_RECORD_MERGE_PROMPT,
    _RecordMergeWork,
    _transcript,
    harder_record_prompt,
)
from maf_compaction._record_replay import RECORD_ANCHOR_EXCLUDE_REASON, source_key
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
