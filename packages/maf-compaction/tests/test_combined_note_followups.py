"""Combining adjacent notes keeps kept merges replaying, host metadata and the recall trigger."""

from __future__ import annotations

import json

import pytest
from agent_framework import ChatContext, ChatResponse, Content, Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    project_included_messages,
)
from test_prompt_recall import recall
from test_record_budget import TOKENIZER, _history, _record_pair, _sent, _strategy, _Summarizer
from test_record_source_window import _note

from maf_compaction import _anchored as anchored
from maf_compaction import _record_replay as replay
from maf_compaction._anchored import EXCLUDE_REASON, MARKER_ID_PREFIX, AnchoredCompactionStrategy
from maf_compaction._artifacts import coalesce_shed_notes, is_marker
from maf_compaction._carryover import CarriedView, Handed
from maf_compaction._composed import DEFAULT_RECORD_MERGE_PROMPT, _transcript
from maf_compaction._saved_view import resume_saved_view, save_view
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


def _colliding_rounds() -> list[Message]:
    """Round ``a`` whose note id a stored message already holds, then round ``a_1``."""
    messages = _history(0)
    closing = messages.pop()
    taken = Message("user", ["Look again."], message_id=f"{MARKER_ID_PREFIX}group_a")
    rounds = []
    for name in ("a", "a_1"):
        rounds += [
            Message(
                "assistant",
                [Content.from_function_call(call_id=name, name="lookup", arguments={})],
                message_id=name,
            ),
            Message(
                "tool",
                [Content.from_function_result(call_id=name, result=f"{name} output")],
                message_id=f"{name}_res",
            ),
        ]
    return [*messages, taken, *_record_pair(0), *rounds, *_record_pair(1), closing]


async def test_a_merge_kept_on_notes_with_collided_ids_still_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shed one pass at a time, round ``a``'s note is ``…group_a_1`` and round ``a_1``'s is
    ``…group_a_1_1``. Splitting their combined note gives those ids back, not ones allocated
    afresh, so a merge kept under the key covering them is still matched."""
    current = replay._source_key
    with monkeypatch.context() as before:
        before.setattr(
            replay,
            "_source_key",
            lambda *args, **kwargs: current(*args, **{**kwargs, "skip_notes": False}),
        )
        before.setattr(anchored, "coalesce_shed_notes", lambda messages: False)
        strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
        messages = _colliding_rounds()
        for name in ("a", "a_1"):
            assert strategy.tool_results.fallback.shed_again(messages, {name, f"{name}_res"})
        ids = [m.message_id for m in messages if is_marker(m)]
        assert ids == [f"{MARKER_ID_PREFIX}group_a_1", f"{MARKER_ID_PREFIX}group_a_1_1"]
        assert await _merge_first_two(strategy, messages) == "accepted"
    merged = _sent_without_notes(messages)

    cold = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    later = _colliding_rounds()
    for name in ("a", "a_1"):
        assert cold.tool_results.fallback.shed_again(later, {name, f"{name}_res"})
    assert sum(is_marker(m) for m in later) == 1, "the premise: the notes are combined"

    assert cold._replay_record_edits(later)
    assert _sent_without_notes(later) == merged


async def test_an_edit_matched_by_the_current_key_never_builds_the_older_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge kept under the key without notes is matched by it, so the list as it was before
    notes were combined is never built, however many notes its window holds."""
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None)
    messages = _two_rounds_between_two_records()
    _shed(strategy, messages, "ta")
    assert await _merge_first_two(strategy, messages) == "accepted"
    _shed(strategy, messages, "tb")
    built: list[int] = []
    split = anchored.split_combined_notes
    monkeypatch.setattr(replay, "split_combined_notes", lambda m: built.append(len(m)) or split(m))

    _assert_replays(strategy, messages)

    assert not built


def test_an_id_a_combined_note_replaced_is_not_given_out_again() -> None:
    """Round ``x_1``'s note ``…group_x_1`` is combined into round ``a``'s. Round ``x``, shed
    later and finding ``…group_x`` taken, gets ``…group_x_2`` as it did before notes were
    combined, not the freed ``…group_x_1``: that would stand twice once the note is split back."""
    messages = _history(0)
    closing = messages.pop()
    taken = Message("user", ["Look again."], message_id=f"{MARKER_ID_PREFIX}group_x")
    messages = [
        *messages,
        taken,
        *_record_pair(0),
        *_named_round("a"),
        *_named_round("x_1"),
        *_record_pair(1),
        *_named_round("x"),
        *_record_pair(2),
        closing,
    ]
    shedding = AnchoredCompactionStrategy(max_input_tokens=4000, tokenizer=TOKENIZER)
    assert shedding.shed_again(messages, {"a", "a_res", "x_1", "x_1_res"})
    assert shedding.shed_again(messages, {"x", "x_res"})

    live = [m.message_id for m in messages if is_marker(m)]
    split = anchored.split_combined_notes(messages)
    assert split is not None
    ids = [m.message_id for m in split[0] if is_marker(m)]

    assert live == [f"{MARKER_ID_PREFIX}group_a", f"{MARKER_ID_PREFIX}group_x_2"]
    assert len(ids) == len(set(ids)) == 3


def _named_round(name: str) -> list[Message]:
    """A tool round whose call message id, and so its group id, is ``name``."""
    return [
        Message(
            "assistant",
            [Content.from_function_call(call_id=name, name="lookup", arguments={})],
            message_id=name,
        ),
        Message(
            "tool",
            [Content.from_function_result(call_id=name, result=f"{name} output")],
            message_id=f"{name}_res",
        ),
    ]


@pytest.mark.parametrize("passes", ["one shed", "two sheds"])
@pytest.mark.parametrize("merged", [False, True], ids=["no merge", "a kept merge"])
async def test_a_saved_view_resumed_across_combined_notes_sends_the_same_request(
    passes: str, merged: bool
) -> None:
    """The rounds between two records shed at once or a pass apart, their notes combined, the
    records merged or not: the view saved from that list is resumed on a fresh copy and sends
    the same messages, in the same order."""
    history = _two_rounds_between_two_records()
    previous = _reloaded(history)
    handed = Handed.take(previous)
    strategy = _strategy(_Summarizer(), budget=100_000, share=0.001, cap=None, policy="shed")
    if passes == "one shed":
        assert strategy.tool_results.fallback.shed_again(
            previous, {"ta_call", "ta_res", "tb_call", "tb_res"}
        )
    else:
        _shed(strategy, previous, "ta")
        _shed(strategy, previous, "tb")
    assert sum(is_marker(m) for m in previous) == 1, "the premise: the notes are combined"
    if merged:
        assert await _merge_first_two(strategy, previous) == "accepted"
    saved = save_view(CarriedView(handed, previous), {})
    assert saved

    current = _reloaded(history)

    assert resume_saved_view(json.loads(json.dumps(saved)), {}, current, Handed.take(current))
    assert _request(current) == _request(previous)


def _reloaded(messages: list[Message]) -> list[Message]:
    return [Message.from_dict(json.loads(m.to_json())) for m in messages]


def _request(messages: list[Message]) -> list[tuple[str | None, str]]:
    return [(m.message_id, m.text) for m in project_included_messages(messages)]
