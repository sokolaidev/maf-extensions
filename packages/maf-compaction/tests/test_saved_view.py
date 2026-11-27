"""A turn can resume from detached history and the previous turn's saved layout."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest
from agent_framework import Agent, AgentSession, Content, InMemoryHistoryProvider, Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    apply_compaction,
    project_included_messages,
)
from test_anchored import _tool_turn
from test_call_carryover import _budget, _Composed, _Layered, _Rebuilding, _registry, _transcript
from test_record_budget import TOKENIZER, _strategy, _Summarizer
from test_user_summary_replay import history as user_history

from maf_compaction import (
    CompactionBudgetExceeded,
    SessionCompactionProvider,
    SessionToolResultRecallMiddleware,
    make_session_record_merge_middleware,
)
from maf_compaction._anchored import AnchoredCompactionStrategy
from maf_compaction._carryover import CarriedView, Handed
from maf_compaction._preserve import set_preserved
from maf_compaction._saved_view import _match, resume_saved_view, save_view
from maf_compaction._toolsummary import build_record_message

pytestmark = pytest.mark.anyio


def _reload(messages: list[Message]) -> list[Message]:
    return [Message.from_dict(json.loads(m.to_json())) for m in messages]


async def _call(
    strategy: _Composed, messages: list[Message], saved: dict[str, Any] | None = None
) -> list[Any]:
    async def compact(messages: list[Message]) -> bool:
        try:
            if saved is not None:
                strategy.restore_saved_view(saved)
            return await strategy.compact_before(messages, options={})
        except CompactionBudgetExceeded:
            return True

    await apply_compaction(messages, strategy=compact)
    return [
        (m.message_id, m.role, [c.to_dict() for c in m.contents])
        for m in project_included_messages(messages)
    ]


@pytest.mark.parametrize("durable", [True, False])
async def test_first_call_with_a_fresh_strategy_matches_full_replay(durable: bool) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    first = _strategy(_Summarizer(), budget=budget, policy="shed")
    host = _Rebuilding(transcript)
    await _call(first, host.hand())
    host.answered(1)
    if durable:
        host.store[-1].message_id = "call-one"
        host.carried_in[0].message_id = "result-one"
    await _call(first, host.hand())
    host.answered(2)
    decisions = json.loads(json.dumps(first.session_state()))
    saved = first.take_saved_view()
    assert saved and not first.carries_view
    assert "You are an assistant." not in json.dumps(saved)
    assert first.session_state() == decisions
    restored = _strategy(_Summarizer(), budget=budget, policy="shed")
    control = _strategy(_Summarizer(), budget=budget, policy="shed")
    for strategy in (control, restored):
        strategy.restore_session_state(copy.deepcopy(decisions))
    handed = host.hand()
    expected = await _call(control, _reload(handed))
    assert await _call(restored, _reload(handed), json.loads(json.dumps(saved))) == expected
    # Records carry durable ids (#2996), so the layout resumes on a transcript numbered by
    # position too; the comparison with a full replay above is what makes that safe.
    assert restored.calls_resumed == 1


#: Large enough for a full replay of the summarized history to fit, so the two can be compared.
_FITS_A_FULL_REPLAY = 6000


def _rebuilt_elsewhere(messages: list[Message]) -> list[Message]:
    """The conversation as a host that keeps its own copy hands it: nothing a pass wrote."""
    return [
        Message(m.role, list(m.contents), message_id=m.message_id, author_name=m.author_name)
        for m in messages
    ]


async def test_saved_view_excludes_stored_and_new_echoes_beside_the_genuine_record() -> None:
    record = build_record_message("Retain the page title.")
    record.message_id = "record"
    history = [
        Message("user", ["Read the page"], message_id="user"),
        record,
        Message("assistant", [record.text], message_id="stored-echo"),
    ]
    first = _strategy(_Summarizer(), budget=_FITS_A_FULL_REPLAY, policy="shed")
    assert [mid for mid, _, _ in await _call(first, _reload(history))] == ["user", "record"]
    saved = first.take_saved_view()
    assert saved
    history.extend(
        [
            Message(
                "assistant", ["[compacted: an earlier assistant reply]"], message_id="new-echo"
            ),
            Message("assistant", ["The page is ready."], message_id="reply"),
        ]
    )
    restored = _strategy(_Summarizer(), budget=_FITS_A_FULL_REPLAY, policy="shed")
    restored.restore_session_state(first.session_state())
    sent = await _call(restored, _reload(history), json.loads(json.dumps(saved)))
    assert [mid for mid, _, _ in sent] == ["user", "record", "reply"]
    assert restored.calls_resumed == 1


async def test_first_call_resumes_when_the_host_hands_back_messages_without_the_links() -> None:
    first = _strategy(_Summarizer(), budget=_FITS_A_FULL_REPLAY, policy="shed")
    host = _Rebuilding(_rebuilt_elsewhere(user_history()))
    await _call(first, host.hand())
    host.answered(1)
    host.store[-1].message_id = "call-one"
    host.carried_in[0].message_id = "result-one"
    await _call(first, host.hand())
    host.answered(2)
    host.store[-1].message_id = "call-two"
    host.carried_in[0].message_id = "result-two"
    decisions = json.loads(json.dumps(first.session_state()))
    saved = first.take_saved_view()
    assert saved
    handed = host.hand()
    assert any(
        SUMMARIZED_BY_SUMMARY_ID_KEY in m.additional_properties.get(GROUP_ANNOTATION_KEY, {})
        for m in handed
    ), "no stored message carries a link, so this proves nothing"
    restored = _strategy(_Summarizer(), budget=_FITS_A_FULL_REPLAY, policy="shed")
    control = _strategy(_Summarizer(), budget=_FITS_A_FULL_REPLAY, policy="shed")
    for strategy in (control, restored):
        strategy.restore_session_state(copy.deepcopy(decisions))

    expected = await _call(control, _rebuilt_elsewhere(handed))
    sent = await _call(restored, _rebuilt_elsewhere(handed), json.loads(json.dumps(saved)))

    assert sent == expected
    assert restored.calls_resumed == 1


@pytest.mark.parametrize("stored_note", [False, True])
@pytest.mark.parametrize("change", ["unchanged", "following role", "mixed attribution"])
async def test_first_call_matches_replay_after_insertions_shift_source_positions(
    stored_note: bool, change: str
) -> None:
    record = build_record_message("Remember.")
    record.message_id = "record"
    history = [
        record,
        Message("user", ["First"], message_id="u0"),
        Message("assistant", ["Earlier answer"], message_id="prefix"),
        Message("user", ["Question"], message_id="u"),
        Message(
            "assistant", [Content.from_function_call("c", "fetch", arguments={})], message_id="call"
        ),
        Message("tool", [Content.from_function_result("c", result="ok")], message_id="result"),
        Message("assistant", ["Answer"], message_id="reply"),
        Message("user", ["Next"], message_id="tail"),
    ]
    first = _strategy(_Summarizer(), budget=6000, policy="shed")
    decisions = first.session_state()
    decisions["shed_ids"] = ["prefix", "call", "result", "reply"]
    first.restore_session_state(decisions)
    previous = _reload(history)
    await _call(first, previous)
    earlier_note = next(m for m in previous if m.message_id == "anchored_group_prefix")
    saved = first.take_saved_view()
    assert saved
    decisions = first.session_state()

    if change == "following role":
        history[-1] = Message("tool", ["New result"], message_id="new")
    elif change == "mixed attribution":
        history[4].additional_properties["_attribution"] = "history"
    if stored_note:
        history.insert(2, earlier_note)
    control = _strategy(_Summarizer(), budget=6000, policy="shed")
    restored = _strategy(_Summarizer(), budget=6000, policy="shed")
    for strategy in (control, restored):
        strategy.restore_session_state(json.loads(json.dumps(decisions)))
    expected = await _call(control, _reload(history))
    assert await _call(restored, _reload(history), json.loads(json.dumps(saved))) == expected
    assert [mid for mid, _, _ in expected].count(earlier_note.message_id) == 1
    if change == "following role":
        texts = next(contents for mid, _, contents in expected if mid == "anchored_group_call")
        assert texts[0]["text"] == "[compacted: 2 earlier conversation groups]"
    elif change == "mixed attribution":
        assert {"call", "result"} <= {mid for mid, _, _ in expected}
    elif not stored_note:
        assert restored.calls_resumed == 1


async def test_edit_to_a_shared_source_before_turn_end_cannot_reuse_its_old_summary() -> None:
    history = user_history()
    first = _strategy(_Summarizer(), budget=3000, policy="shed")
    handed = [copy.copy(message) for message in history]
    for message in handed:
        message.additional_properties = {}
    initial = await _call(first, handed)
    assert "u1" not in {mid for mid, _, _ in initial}
    assert any(mid == "user_summary_0" for mid, _, _ in initial)
    history[1].contents[0].text = "Edited requirement."
    decisions = first.session_state()
    saved = first.take_saved_view()
    control = _strategy(_Summarizer(), budget=3000, policy="shed")
    restored = _strategy(_Summarizer(), budget=3000, policy="shed")
    for strategy in (control, restored):
        strategy.restore_session_state(json.loads(json.dumps(decisions)))
    expected = await _call(control, _reload(history))
    assert await _call(restored, _reload(history), saved) == expected
    assert restored.calls_resumed == 0


def _layout() -> tuple[dict[str, Any], list[Message], Message]:
    source = Message("user", ["private transcript"], message_id="original")
    last = Message("user", ["tail"], message_id="tail")
    handed = Handed.take([source, last])
    kept = copy.copy(source)
    kept.additional_properties = {
        EXCLUDED_KEY: True,
        EXCLUDE_REASON_KEY: "tool_summary_consolidated",
        "_preserved": True,
        "_preserve_reason": "tool_summary_uncovered",
    }
    record = build_record_message("Remember 17.")
    saved = save_view(CarriedView(handed, [kept, record, last]), {})
    assert saved and "private transcript" not in json.dumps(saved)
    return json.loads(json.dumps(saved)), [source, last], record


@pytest.mark.parametrize("before_save", [True, False])
async def test_a_late_host_hold_retains_the_entire_tool_group(before_save: bool) -> None:
    original = [
        Message(
            "assistant", [Content.from_function_call("c", "fetch", arguments={})], message_id="call"
        ),
        Message("tool", [Content.from_function_result("c", result="ok")], message_id="result"),
    ]
    first = _strategy(_Summarizer(), budget=6000, policy="shed")
    seed = first.session_state()
    seed["shed_ids"] = ["call", "result"]
    first.restore_session_state(seed)
    previous = _reload(original)
    initial = await _call(first, previous)
    assert [mid for mid, _, _ in initial] == ["anchored_group_call"]
    if before_save:
        result = next(m for m in previous if m.message_id == "result")
        set_preserved(result, preserved=True, reason="host")
    saved = first.take_saved_view()
    set_preserved(original[1], preserved=True, reason="host")
    control = _strategy(_Summarizer(), budget=6000, policy="shed")
    restored = _strategy(_Summarizer(), budget=6000, policy="shed")
    for strategy in (control, restored):
        strategy.restore_session_state(json.loads(json.dumps(first.session_state())))
    expected = await _call(control, _reload(original))
    assert [mid for mid, _, _ in expected] == ["call", "result"]
    assert await _call(restored, _reload(original), saved) == expected
    assert restored.calls_resumed == 0


@pytest.mark.parametrize("change", ["source properties", "record text", "record id"])
def test_live_and_saved_views_reject_edits_to_the_completed_layout(change: str) -> None:
    source = Message("user", ["Question"], message_id="u")
    source.additional_properties["host"] = {"revision": 1}
    record = build_record_message("Original record.")
    carried = CarriedView(Handed.take([source]), [source, record])
    if change == "source properties":
        source.additional_properties["host"]["revision"] = 2
    elif change == "record text":
        record.contents[0].text = "Edited record."
    else:
        record.message_id = "new-record"
    assert save_view(carried, {}) is None
    for messages in ([source, record], _reload([source])):
        assert carried.resume(messages, Handed.take(messages)) is None


def test_changed_tail_resumes_only_after_every_replacement() -> None:
    saved, messages, record = _layout()
    messages[-1] = Message("user", ["edited tail"])
    loaded = _reload(messages)
    current = Handed.take(loaded)
    assert resume_saved_view(saved, {}, loaded, current)
    assert [m.text for m in loaded] == ["private transcript", record.text, "edited tail"]
    assert loaded[0].additional_properties[EXCLUDED_KEY]
    assert "_preserved" not in loaded[0].additional_properties


def test_visible_shared_edit_can_resume_live_but_cannot_certify_a_later_turn() -> None:
    source = Message("user", ["Before"])
    view = CarriedView(Handed.take([source]), [source])
    source.contents[0].text = "After"
    for _ in range(2):
        loaded = [copy.copy(source)]
        handed = Handed.take(loaded)
        assert view.resume(loaded, handed)
        assert loaded[0].text == "After"
        view = CarriedView(handed, loaded)
        assert save_view(view, {}) is None


async def test_a_pass_over_the_same_edited_objects_cannot_save_the_old_source_digests() -> None:
    messages = user_history()
    strategy = _strategy(_Summarizer(), budget=3000, policy="shed")
    await _call(strategy, messages)
    source = next(message for message in messages if message.message_id == "u1")
    original = source.text
    source.contents[0].text = "A changed requirement."
    await _call(strategy, messages)
    source.contents[0].text = original
    assert strategy.take_saved_view() is None


@pytest.mark.parametrize("retained", [True, False])
def test_a_new_record_cannot_be_saved_under_a_source_edited_during_the_pass(retained: bool) -> None:
    source = Message("user", ["Original text"], message_id="u")
    handed = Handed.take([source])
    source.contents[0].text = "Edited text"
    record = build_record_message(source.text)
    view = [source, record] if retained else [record]
    saved = save_view(CarriedView(handed, view), {})
    assert saved is None


@pytest.mark.parametrize("substituted", [False, True])
@pytest.mark.parametrize("edit", ["replace list", "replace item", "edit item", "clear list"])
def test_a_removed_source_must_still_match_at_pass_completion(substituted: bool, edit: str) -> None:
    source = Message("user", ["Original"], message_id="u")
    handed = Handed.take([source])
    used = copy.copy(source) if substituted else source
    handed.stood[0] = used
    if edit == "replace list":
        used.contents = [Content.from_text("Changed")]
    elif edit == "replace item":
        used.contents[0] = Content.from_text("Changed")
    elif edit == "edit item":
        used.contents[0].text = "Changed"
    else:
        used.contents.clear()
    record = build_record_message(f"Source read as {used.text!r}.")
    carried = CarriedView(handed, [record])
    handed.contents[0][0].text = "Original"
    used.contents = list(handed.contents[0])
    assert save_view(carried, {}) is None


@pytest.mark.parametrize("original_tail", [True, False])
def test_changed_following_message_replays_the_reply_note(original_tail: bool) -> None:
    sources = [
        Message("user", ["Question"], message_id="u"),
        Message("assistant", ["Answer"], message_id="a"),
    ]
    if original_tail:
        sources.append(Message("user", ["Next"], message_id="next"))
    previous = _reload(sources)
    handed = Handed.take(previous)
    anchored = AnchoredCompactionStrategy(max_input_tokens=4000, tokenizer=TOKENIZER)
    assert anchored.shed_again(previous, ["a"])
    assert any("already answered" in m.text for m in previous)
    saved = save_view(CarriedView(handed, previous), {})
    assert saved
    current = [
        *_reload(sources[:2]),
        Message("system", ["Instructions"]),
        Message("tool", ["Result"]),
    ]
    before = [m.to_dict() for m in current]
    assert resume_saved_view(saved, {}, current, Handed.take(current)) is None
    assert [m.to_dict() for m in current] == before
    assert anchored.shed_again(current, ["a"])
    assert any(m.text == "[compacted: an earlier assistant reply]" for m in current)


def test_a_saved_tool_run_validates_every_source_group() -> None:
    history = [m for i in range(3) for m in _tool_turn(i, 10)[:2]]
    previous = _reload(history)
    handed = Handed.take(previous)
    anchored = AnchoredCompactionStrategy(max_input_tokens=4000, tokenizer=TOKENIZER)
    assert anchored.shed_again(previous, {m.message_id for m in previous if m.message_id})
    [note] = project_included_messages(previous)
    saved = save_view(CarriedView(handed, previous), {})
    assert saved
    current = _reload(history)
    assert resume_saved_view(saved, {}, current, Handed.take(current))
    assert [m.text for m in project_included_messages(current)] == [note.text]

    current = _reload(history)
    current[-1].additional_properties["_attribution"] = "loaded"
    before = [m.to_dict() for m in current]
    assert resume_saved_view(saved, {}, current, Handed.take(current)) is None
    assert [m.to_dict() for m in current] == before


@pytest.mark.parametrize("change", ["mixed attribution", "later result", "larger group"])
def test_cached_tool_exclusion_requires_a_still_removable_whole_group(change: str) -> None:
    sources = [
        Message(
            "assistant", [Content.from_function_call("c", "fetch", arguments={})], message_id="call"
        ),
        Message("tool", [Content.from_function_result("c", result="result")], message_id="result"),
    ]
    if change == "later result":
        sources[0].contents.append(Content.from_function_call("pending", "fetch", arguments={}))
    previous = _reload(sources)
    handed = Handed.take(previous)
    anchored = AnchoredCompactionStrategy(max_input_tokens=4000, tokenizer=TOKENIZER)
    assert anchored.shed_again(previous, ["call", "result"])
    saved = save_view(CarriedView(handed, previous), {})
    assert saved
    current = _reload(sources)
    if change == "mixed attribution":
        current[0].additional_properties["_attribution"] = "history"
    elif change == "later result":
        current.extend(
            [
                Message("user", ["Next"]),
                Message("tool", [Content.from_function_result("pending", result="later")]),
            ]
        )
    else:
        current.append(Message("tool", ["Another result"]))
    before = [m.to_dict() for m in current]
    assert resume_saved_view(saved, {}, current, Handed.take(current)) is None
    assert [m.to_dict() for m in current] == before
    assert not anchored.shed_again(current, ["call", "result"])


@pytest.mark.parametrize("omitted", [True, False])
def test_saved_record_cannot_remove_a_mixed_attribution_group(omitted: bool) -> None:
    sources = [
        Message("assistant", [Content.from_function_call("c", "fetch", arguments={})]),
        Message("tool", [Content.from_function_result("c", result="ok")]),
    ]
    handed = Handed.take(sources)
    kept = [] if omitted else [copy.copy(m) for m in sources]
    for m in kept:
        m.additional_properties = {
            EXCLUDED_KEY: True,
            EXCLUDE_REASON_KEY: "tool_summary_consolidated",
        }
    saved = save_view(CarriedView(handed, [*kept, build_record_message("Remember.")]), {})
    assert saved
    loaded = _reload(sources)
    assert resume_saved_view(saved, {}, loaded, Handed.take(loaded))
    loaded = _reload(sources)
    loaded[0].additional_properties["_attribution"] = "history"
    before = [m.to_dict() for m in loaded]
    assert resume_saved_view(saved, {}, loaded, Handed.take(loaded)) is None
    assert [m.to_dict() for m in loaded] == before


@pytest.mark.parametrize(
    "change", ["source", "host-hold", "host-exclusion", "decisions", "damaged", "legacy"]
)
def test_unusable_layout_leaves_handed_messages_intact(change: str) -> None:
    saved, messages, _ = _layout()
    decisions = {}
    if change == "source":
        messages[0] = Message("user", ["replacement"])
    if change == "host-hold":
        messages[0].additional_properties["_preserved"] = True
    if change == "host-exclusion":
        messages[0].additional_properties.update({EXCLUDED_KEY: True, EXCLUDE_REASON_KEY: "host"})
    if change == "decisions":
        decisions["new"] = True
    if change == "damaged":
        saved["layout"] = saved["layout"][:-2]
    if change == "legacy":
        saved["version"] = 4
    before = [m.to_dict() for m in messages]
    assert resume_saved_view(saved, decisions, messages, Handed.take(messages)) is None
    assert [m.to_dict() for m in messages] == before


def test_a_record_kept_by_the_store_reappears_once_across_turns() -> None:
    saved, sources, record = _layout()
    stored = [sources[0], record, sources[1]]
    for _ in range(4):
        loaded = _reload(stored)
        handed = Handed.take(loaded)
        assert resume_saved_view(saved, {}, loaded, handed)
        assert [m.text for m in loaded].count(record.text) == 1
        next_saved = save_view(CarriedView(handed, loaded), {})
        assert next_saved
        saved = next_saved


def test_only_the_exclusion_written_on_this_source_is_accepted() -> None:
    saved, messages, _ = _layout()
    messages[0].additional_properties.update(
        {EXCLUDED_KEY: True, EXCLUDE_REASON_KEY: "tool_summary_consolidated"}
    )
    assert resume_saved_view(saved, {}, list(messages), Handed.take(messages))
    messages[0].additional_properties[EXCLUDE_REASON_KEY] = "user_turn_summarized"
    assert resume_saved_view(saved, {}, messages, Handed.take(messages)) is None


def test_a_source_is_accepted_without_the_links_the_layout_restores_and_with_no_others() -> None:
    def conversation(annotation: dict[str, Any] | None) -> list[Message]:
        source = Message("user", ["requirement"], message_id="original")
        if annotation is not None:
            source.additional_properties[GROUP_ANNOTATION_KEY] = annotation
        return [source, Message("user", ["tail"], message_id="tail")]

    linked = {SUMMARIZED_BY_SUMMARY_ID_KEY: "summary"}
    kept = conversation(linked)
    saved = save_view(CarriedView(Handed.take(kept), kept), {})
    assert saved
    saved = json.loads(json.dumps(saved))

    bare = conversation(None)
    assert resume_saved_view(saved, {}, bare, Handed.take(bare))
    assert bare[0].additional_properties[GROUP_ANNOTATION_KEY] == linked
    for annotation in ({SUMMARIZED_BY_SUMMARY_ID_KEY: "another"}, {"host_note": 1}):
        changed = conversation(annotation)
        assert resume_saved_view(saved, {}, changed, Handed.take(changed)) is None


def test_a_store_that_stripped_a_generated_records_identity_gets_full_replay() -> None:
    saved, messages, record = _layout()
    bare = Message(record.role, record.contents)
    for loaded in ([messages[0], bare, messages[1]], [*messages, Message("user", ["new"]), bare]):
        assert resume_saved_view(saved, {}, loaded, Handed.take(loaded)) is None


@pytest.mark.parametrize("record", [False, True])
def test_insert_slots_accept_only_single_text_generated_messages(record: bool) -> None:
    saved, messages, _ = _layout()
    entry = build_record_message("Remember.") if record else Message("user", ["Stored source"])
    if record:
        entry.contents.append(Content.from_text("Another part"))
    layout = json.loads(saved["layout"])
    insert = next(item for item in layout["order"] if "insert" in item)
    insert["insert"] = entry.to_dict()
    assert _match(layout, Handed.take(messages)) is None
    assert save_view(CarriedView(Handed.take(messages), [*messages, entry]), {}) is None


async def test_a_storage_pass_that_changes_decisions_cannot_save_the_old_view() -> None:
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.6), policy="shed")
    await _call(strategy, transcript)
    assert strategy.carries_view
    before = strategy.session_state()
    await strategy(_transcript(9, 8, 15))
    assert strategy.session_state() != before
    assert strategy.take_saved_view() is None
    assert not strategy.carries_view


def test_a_displaced_record_or_length_dependent_id_gets_a_full_replay() -> None:
    saved, messages, record = _layout()
    moved = [record, *messages]
    assert resume_saved_view(saved, {}, moved, Handed.take(moved)) is None
    record.message_id = "msg_120"
    handed = Handed.take(messages)
    saved = save_view(CarriedView(handed, [messages[0], record, messages[1]]), {})
    assert saved
    loaded = [*_reload(messages), Message("user", ["new"])]
    assert resume_saved_view(saved, {}, loaded, Handed.take(loaded)) is None


@pytest.mark.parametrize("tail_size", [1, 2])
async def test_direct_restoration_keeps_the_ids_full_replay_assigns(tail_size: int) -> None:
    history = _transcript(3, 0, 0)
    budget = _budget(history, 0.6)
    first = _strategy(_Summarizer(), budget=budget, policy="shed")
    await first.compact_before(_reload(history), options={})
    saved = first.take_saved_view()
    assert saved
    history.extend(Message("user", [f"Tail {i}"]) for i in range(tail_size))
    outputs = []
    for warm in (False, True):
        strategy = _strategy(_Summarizer(), budget=budget, policy="shed")
        strategy.restore_session_state(copy.deepcopy(first.session_state()))
        if warm:
            strategy.restore_saved_view(saved)
        messages = _reload(history)
        await strategy.compact_before(messages, options={})
        if warm:
            # Records carry durable ids (#2996): a tail numbered by position no longer collides
            # with the records' numbers, so the layout resumes and must send what replay sends.
            assert strategy.calls_resumed == 1
        outputs.append(
            [
                (m.message_id, m.role, m.to_dict()["contents"])
                for m in project_included_messages(messages)
            ]
        )
    assert outputs[1] == outputs[0]


@pytest.mark.parametrize("ending", ["provider", "host-release", "merge-middleware"])
async def test_session_writes_one_separate_cache_at_turn_end(ending: str) -> None:
    transcript = _transcript(14, 10, 6)
    transcript.append(Message("assistant", ["I have the results."], message_id="results"))
    budget = _budget(transcript, 0.6)
    key = "turn_view"
    session = AgentSession()
    registry = _registry(budget, [], saved_view_key=key)
    providers: list[Any] = [InMemoryHistoryProvider()]
    middleware: list[Any] = [SessionToolResultRecallMiddleware(registry, compact_before=True)]
    if ending == "provider":
        providers.append(
            SessionCompactionProvider(registry, tokenizer=TOKENIZER, before=False, after=False)
        )
    if ending == "merge-middleware":
        middleware.insert(0, make_session_record_merge_middleware(registry))
    agent = Agent(
        client=_Layered(),
        context_providers=providers,
        middleware=middleware,
    )
    await agent.run(transcript, session=session)
    if ending == "host-release":
        assert key not in session.state
        registry.release(session.session_id)
    saved = session.state[key]
    assert saved["version"] == 5
    assert "layout" not in json.dumps(session.state[registry.state_key])
    registry.release(session.session_id)
    assert session.state[key] is saved

    envelope = json.dumps(session.to_dict())
    sent = []
    for warm in (True, False):
        loaded = AgentSession.from_dict(json.loads(envelope))
        if not warm:
            del loaded.state[key]
        built: list[_Composed] = []
        fresh = _registry(budget, built, saved_view_key=key)
        client = _Layered()
        replica = Agent(
            client=client,
            context_providers=[InMemoryHistoryProvider()],
            middleware=[SessionToolResultRecallMiddleware(fresh, compact_before=True)],
        )
        await replica.run(Message("user", ["Continue."], message_id="another"), session=loaded)
        assert built[0].calls_resumed == int(warm)
        assert key not in loaded.state
        sent.append(client.sent)
    assert sent[0] == sent[1]


@pytest.mark.parametrize("save_fails", [False, True])
async def test_record_merge_run_keeps_ownership_until_turn_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch, save_fails: bool
) -> None:
    registry = _registry(6000, [], saved_view_key="turn_view")
    session = AgentSession()
    agent = Agent(
        client=_Layered(),
        middleware=[
            make_session_record_merge_middleware(registry),
            SessionToolResultRecallMiddleware(registry, compact_before=True),
        ],
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    end_turn = registry._end_turn

    async def delayed_save(session_id: str) -> None:
        if not entered.is_set():
            entered.set()
            await finish.wait()
            if save_fails:
                raise ValueError("save failed")
        await end_turn(session_id)

    monkeypatch.setattr(registry, "_end_turn", delayed_save)
    first = asyncio.ensure_future(agent.run("first", session=session))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        with pytest.raises(RuntimeError, match="Overlapping agent runs"):
            await agent.run("overlap", session=session)
    finally:
        finish.set()
        if save_fails:
            with pytest.raises(ValueError, match="save failed"):
                await first
        else:
            await first
    await agent.run("next", session=session)
