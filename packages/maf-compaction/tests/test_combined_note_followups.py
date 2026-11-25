"""Combining adjacent notes keeps kept merges replaying, host metadata and the recall trigger."""

from __future__ import annotations

import json

import pytest
from agent_framework import ChatContext, ChatResponse, Content, Message
from agent_framework._compaction import EXCLUDE_REASON_KEY, EXCLUDED_KEY
from test_prompt_recall import recall
from test_record_budget import _history, _record_pair, _sent, _strategy, _Summarizer
from test_record_source_window import _note

from maf_compaction import _anchored as anchored
from maf_compaction import _record_replay as replay
from maf_compaction._anchored import EXCLUDE_REASON
from maf_compaction._artifacts import coalesce_shed_notes, is_marker
from maf_compaction._composed import DEFAULT_RECORD_MERGE_PROMPT, _transcript
from maf_compaction._toolsummary import consolidatable_record_groups

pytestmark = pytest.mark.anyio


def _round(name: str) -> list[Message]:
    return [
        Message(
            "assistant",
            [Content.from_function_call(call_id=name, name="lookup", arguments={})],
            message_id=f"{name}_call",
        ),
        Message(
            "tool",
            [Content.from_function_result(call_id=name, result=f"{name} output")],
            message_id=f"{name}_res",
        ),
    ]


def _two_rounds_between_two_records() -> list[Message]:
    messages = _history(0)
    closing = messages.pop()
    return [*messages, *_record_pair(0), *_round("ta"), *_round("tb"), *_record_pair(1), closing]


def _shed(strategy, messages: list[Message], name: str) -> None:
    assert strategy.tool_results.fallback.shed_again(messages, {f"{name}_call", f"{name}_res"})


async def test_a_kept_merge_replays_when_its_notes_are_combined_after_it() -> None:
    """A round between the records is shed before the merge and the next one after it, so the
    replay finds their notes combined into one the merge never saw, and puts it back."""
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    messages = _two_rounds_between_two_records()
    _shed(strategy, messages, "ta")
    assert await _merge_first_two(strategy, messages) == "accepted"
    _shed(strategy, messages, "tb")
    assert sum(is_marker(m) for m in messages) == 1, "the premise: the two notes are one"

    _assert_replays(strategy, messages)


async def test_a_merge_kept_before_notes_were_combined_or_skipped_still_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both rounds were shed before the merge, each with its own note, and the merge was kept
    under the key that covered those notes. Replayed where the two notes are now one, it is
    matched on the notes as they were and put back."""
    current = replay._source_key
    with monkeypatch.context() as before:
        before.setattr(
            replay,
            "_source_key",
            lambda *args, **kwargs: current(*args, **{**kwargs, "skip_notes": False}),
        )
        before.setattr(anchored, "coalesce_shed_notes", lambda messages: False)
        strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
        messages = _two_rounds_between_two_records()
        _shed(strategy, messages, "ta")
        _shed(strategy, messages, "tb")
        assert sum(is_marker(m) for m in messages) == 2, "the premise: a note per round"
        assert await _merge_first_two(strategy, messages) == "accepted"

    _assert_replays(strategy, messages)


async def _merge_first_two(strategy, messages: list[Message]) -> str:
    groups = consolidatable_record_groups(messages)[:2]
    return await strategy._consolidate(
        messages,
        groups,
        prompt=DEFAULT_RECORD_MERGE_PROMPT,
        transcript=_transcript(messages, groups),
    )


def _assert_replays(strategy, messages: list[Message]) -> None:
    """Restored, with both rounds shed and their notes combined, the merge is put back."""
    merged = _sent_without_notes(messages)
    cold = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    later = _two_rounds_between_two_records()
    _shed(cold, later, "ta")
    _shed(cold, later, "tb")
    assert sum(is_marker(m) for m in later) == 1, "the premise: the notes are combined"

    assert cold._replay_record_edits(later)
    assert _sent_without_notes(later) == merged


def _sent_without_notes(messages: list[Message]) -> list[str]:
    """What the model is sent, but for the notes: the two lists hold them combined differently."""
    return [str(contents) for _, contents in _sent(messages) if "[compacted:" not in str(contents)]


def test_a_note_whose_text_carries_metadata_is_not_combined() -> None:
    """A combined note is a fresh text item, so a note carrying anything else stays whole."""
    plain = [_note(0), _note(1)]
    labelled = [_note(0), _note(1)]
    labelled[1].contents[0].additional_properties["security_label"] = "restricted"

    assert coalesce_shed_notes(plain) and len(plain) == 1
    assert not coalesce_shed_notes(labelled)
    assert labelled[1].contents[0].additional_properties["security_label"] == "restricted"


async def test_combining_the_notes_before_the_newest_record_still_forces_a_due_record() -> None:
    """Combining two notes moves the newest record back one place. With its index taken before,
    the trigger counts from the first pending round and finds one too few to ask again."""
    messages = [Message("user", ["Look everything up."], message_id="task")]
    for index in range(2):
        shed = _round(f"s{index}")
        for message in shed:
            message.additional_properties[EXCLUDED_KEY] = True
            message.additional_properties[EXCLUDE_REASON_KEY] = EXCLUDE_REASON
        messages += [*shed, _note(index)]
    messages += _record_pair(0)
    for index in range(4):
        pending = _round(f"big{index}")
        pending[1].contents[0].result = "row " * 400
        messages += pending
    middleware, _ = recall()
    middleware._force_next = False
    context = ChatContext(None, messages, {})

    async def answer() -> None:
        context.result = ChatResponse(messages=Message("assistant", ["answer"]))

    await middleware.process(context, answer)

    assert middleware._force_next
