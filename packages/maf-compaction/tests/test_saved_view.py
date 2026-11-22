"""A turn can resume from detached history and the previous turn's saved layout."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from agent_framework import Agent, AgentSession, Content, InMemoryHistoryProvider, Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    apply_compaction,
    project_included_messages,
)
from test_call_carryover import _budget, _Composed, _Layered, _Rebuilding, _registry, _transcript
from test_record_budget import TOKENIZER, _strategy, _Summarizer

from maf_compaction import (
    CompactionBudgetExceeded,
    SessionCompactionProvider,
    SessionToolResultRecallMiddleware,
    make_session_record_merge_middleware,
)
from maf_compaction._anchored import AnchoredCompactionStrategy
from maf_compaction._carryover import CarriedView, Handed
from maf_compaction._saved_view import resume_saved_view, save_view
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
    assert restored.calls_resumed == int(durable)


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


def test_changed_tail_resumes_only_after_every_replacement() -> None:
    saved, messages, record = _layout()
    messages[-1] = Message("user", ["edited tail"])
    loaded = _reload(messages)
    current = Handed.take(loaded)
    assert resume_saved_view(saved, {}, loaded, current)
    assert [m.text for m in loaded] == ["private transcript", record.text, "edited tail"]
    assert loaded[0].additional_properties[EXCLUDED_KEY]
    assert "_preserved" not in loaded[0].additional_properties


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
    "change", ["source", "host-hold", "host-exclusion", "decisions", "damaged"]
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


def test_a_store_that_stripped_a_generated_records_identity_gets_full_replay() -> None:
    saved, messages, record = _layout()
    bare = Message(record.role, record.contents)
    for loaded in ([messages[0], bare, messages[1]], [*messages, Message("user", ["new"]), bare]):
        assert resume_saved_view(saved, {}, loaded, Handed.take(loaded)) is None


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
    assert saved["version"] == 1
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
