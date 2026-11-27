"""Passes persist the view through a store and another process resumes from it (#2995)."""

from __future__ import annotations

import logging
import threading
from collections import Counter
from typing import Any, cast

import pytest
from agent_framework import AgentSession, Message
from agent_framework._compaction import apply_compaction, project_included_messages
from test_call_carryover import _budget, _Composed, _Rebuilding, _registry, _transcript

from maf_compaction import CompactionBudgetExceeded, InMemoryViewStore, ViewKey, ViewPart

pytestmark = pytest.mark.anyio

_LOGGER = "maf_compaction"


class _CountingStore(InMemoryViewStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes: Counter[ViewPart] = Counter()

    async def write(self, key: ViewKey, data: dict[str, Any], *, etag: str | None) -> str:
        self.writes[key.part] += 1
        return await super().write(key, data, etag=etag)


async def _pass(registry: Any, session: AgentSession, messages: list[Message]) -> list[Any]:
    """One per-call pass as the session middleware runs it, with the view loaded and written."""

    async def compact(messages: list[Message]) -> bool:
        with registry.activate(session) as stack:
            strategy = cast(_Composed, stack.strategy)
            await registry._load_view(session.session_id, strategy)
            try:
                return await strategy.compact_before(messages, options={})
            except CompactionBudgetExceeded:
                return True
            finally:
                await registry._persist_view(session.session_id, strategy)

    await apply_compaction(messages, strategy=compact)
    return [
        (m.message_id, m.role, [c.to_dict() for c in m.contents])
        for m in project_included_messages(messages)
    ]


def _modes(caplog: pytest.LogCaptureFixture) -> list[str]:
    prefix = "Compaction pass: mode="
    return [
        r.getMessage()[len(prefix) :].split(";")[0]
        for r in caplog.records
        if r.getMessage().startswith(prefix)
    ]


async def test_another_process_resumes_from_the_stored_view_and_sends_the_same(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="moved")
    first = _registry(budget, [], view_store=store)
    host = _Rebuilding(transcript)
    await _pass(first, session, host.hand())
    host.answered(1)

    # The same next call on the first process, which still carries its view in memory.
    kept_registry = _registry(budget, [], view_store=InMemoryViewStore())
    kept_session = AgentSession(session_id="kept")
    kept_host = _Rebuilding(transcript)
    await _pass(kept_registry, kept_session, kept_host.hand())
    kept_host.answered(1)
    expected = await _pass(kept_registry, kept_session, kept_host.hand())

    # A second process: the session's state, the shared store, and no view in memory.
    second = _registry(budget, [], view_store=store)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        sent = await _pass(second, session, host.hand())

    assert sent == expected
    assert _modes(caplog) == ["persisted view restored"]


async def test_the_earlier_part_is_written_again_only_when_it_changes() -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = _CountingStore()
    session = AgentSession(session_id="writes")
    registry = _registry(budget, [], view_store=store)
    host = _Rebuilding(transcript)

    for call in range(1, 5):
        await _pass(registry, session, host.hand())
        host.answered(call)

    assert store.writes[ViewPart.CURRENT] == 4
    assert store.writes[ViewPart.EARLIER] < store.writes[ViewPart.CURRENT]


async def test_a_view_bound_to_other_decisions_is_not_offered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="stale")
    await _pass(_registry(budget, [], view_store=store), session, _Rebuilding(transcript).hand())
    state = session.state["maf_compaction"]
    # A decision made after the parts were written: here, one more shed id.
    state["strategy"]["shed_ids"] = [*state["strategy"]["shed_ids"], "phantom"]

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _pass(
            _registry(budget, [], view_store=store), session, _Rebuilding(transcript).hand()
        )

    assert any("it is bound to other decisions" in r.getMessage() for r in caplog.records)
    assert _modes(caplog)[0].startswith("full")


async def test_the_host_can_invalidate_a_stored_view() -> None:
    transcript = _transcript(14, 10, 6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="invalidated")
    registry = _registry(_budget(transcript, 0.6), [], view_store=store)
    await _pass(registry, session, _Rebuilding(transcript).hand())

    await registry.invalidate_view(session.session_id)

    for part in ViewPart:
        assert await store.read(ViewKey(session.session_id, "agent", part)) is None


async def test_a_failing_store_never_fails_the_pass(caplog: pytest.LogCaptureFixture) -> None:
    class _Broken(InMemoryViewStore):
        async def read(self, key: ViewKey) -> Any:
            raise OSError("unreachable")

        async def write(self, key: ViewKey, data: dict[str, Any], *, etag: str | None) -> str:
            raise OSError("unreachable")

    transcript = _transcript(14, 10, 6)
    session = AgentSession(session_id="broken")
    registry = _registry(_budget(transcript, 0.6), [], view_store=_Broken())

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        sent = await _pass_guarded(registry, session, _Rebuilding(transcript).hand())

    assert sent
    # The read fails; with nothing observed the write is not attempted.
    assert sum("view store failed (OSError)" in r.getMessage() for r in caplog.records) == 1
    assert any("versions were not observed" in r.getMessage() for r in caplog.records)


async def _pass_guarded(registry: Any, session: AgentSession, messages: list[Message]) -> list[Any]:
    """:func:`_pass` through the guard the session middleware wraps view I/O in."""
    from maf_compaction._session import _guarded_view_io

    async def compact(messages: list[Message]) -> bool:
        with registry.activate(session) as stack:
            strategy = cast(_Composed, stack.strategy)
            await _guarded_view_io(registry._load_view(session.session_id, strategy))
            try:
                return await strategy.compact_before(messages, options={})
            except CompactionBudgetExceeded:
                return True
            finally:
                await _guarded_view_io(registry._persist_view(session.session_id, strategy))

    await apply_compaction(messages, strategy=compact)
    return list(project_included_messages(messages))


def test_a_linked_record_gets_the_same_durable_id_each_time_and_an_unlinked_one_none() -> None:
    from maf_compaction._artifacts import RECORD_ID_PREFIX
    from maf_compaction._record_replay import LINEAGE_KEY
    from maf_compaction._toolsummary import build_record_message, identify_record

    def linked(lineage: str) -> Message:
        record = build_record_message("Record: value-1 value-2")
        record.additional_properties[LINEAGE_KEY] = lineage
        return identify_record(record)

    first, again, other = linked("abc"), linked("abc"), linked("xyz")
    unlinked = identify_record(build_record_message("Record: value-1 value-2"))

    assert first.message_id == again.message_id
    assert str(first.message_id).startswith(RECORD_ID_PREFIX)
    assert other.message_id != first.message_id
    assert unlinked.message_id is None


async def test_a_view_published_during_a_pass_makes_its_write_conflict_and_drop_its_view(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    slow, fast = _registry(budget, [], view_store=store), _registry(budget, [], view_store=store)
    session, other = AgentSession(session_id="race"), AgentSession(session_id="race")

    with slow.activate(session) as stack:
        strategy = cast(_Composed, stack.strategy)
        await slow._load_view("race", strategy)  # observes nothing stored
        await _pass(fast, other, _Rebuilding(transcript).hand())  # publishes meanwhile
        published = await store.read(ViewKey("race", "agent", ViewPart.CURRENT))
        await apply_compaction(_Rebuilding(transcript).hand(), strategy=_compactor(strategy))
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await slow._persist_view("race", strategy)
        assert not strategy.carries_view

    kept = await store.read(ViewKey("race", "agent", ViewPart.CURRENT))
    assert published is not None and kept is not None and kept.etag == published.etag
    assert any("another writer's is newer" in r.getMessage() for r in caplog.records)
    assert "race" not in slow._views


def _compactor(strategy: _Composed) -> Any:
    async def compact(messages: list[Message]) -> bool:
        try:
            return await strategy.compact_before(messages, options={})
        except CompactionBudgetExceeded:
            return True

    return compact


async def test_a_view_written_under_other_settings_is_not_offered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="settings")
    await _pass(_registry(budget, [], view_store=store), session, _Rebuilding(transcript).hand())
    reconfigured = _registry(budget, [], view_store=store)

    with reconfigured.activate(session) as stack:
        reconfiguring = cast(_Composed, stack.strategy)
        reconfiguring.user_turns.prompt = "Summarize differently."
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await reconfigured._load_view("settings", reconfiguring)
        assert not reconfiguring.has_saved_view

    assert any("other decisions or settings" in r.getMessage() for r in caplog.records)


def test_a_layout_json_refuses_is_no_saved_view_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maf_compaction import _saved_view

    monkeypatch.setattr(
        _saved_view,
        "_layout_data",
        lambda carried: {"sources": [], "order": [{"insert": {"v": float("nan")}}], "required": 0},
    )

    assert _saved_view.save_view(cast(Any, object()), {}) is None


async def test_release_and_eviction_drop_what_was_known_of_a_sessions_view() -> None:
    transcript = _transcript(14, 10, 6)
    registry = _registry(
        _budget(transcript, 0.6), [], view_store=InMemoryViewStore(), max_cached_sessions=1
    )
    first, second = AgentSession(session_id="first"), AgentSession(session_id="second")
    await _pass(registry, first, _Rebuilding(transcript).hand())
    assert "first" in registry._views

    await _pass(registry, second, _Rebuilding(transcript).hand())
    assert "first" not in registry._views
    registry.release("second")
    assert registry._views == {}


async def test_the_layout_is_built_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = _transcript(14, 10, 6)
    loop_thread = threading.get_ident()
    threads: list[int] = []
    original = _Composed.persisted_view_parts

    def recorded(self: _Composed, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return original(self, **kwargs)

    monkeypatch.setattr(_Composed, "persisted_view_parts", recorded)
    registry = _registry(_budget(transcript, 0.6), [], view_store=InMemoryViewStore())
    await _pass(registry, AgentSession(session_id="off"), _Rebuilding(transcript).hand())

    assert threads and all(thread != loop_thread for thread in threads)


async def test_after_invalidation_the_next_pass_rebuilds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = _transcript(14, 10, 6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="rebuilt")
    registry = _registry(_budget(transcript, 0.6), [], view_store=store)
    host = _Rebuilding(transcript)
    await _pass(registry, session, host.hand())
    host.answered(1)

    await registry.invalidate_view("rebuilt")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _pass(registry, session, host.hand())

    assert _modes(caplog) == ["full (nothing carried or saved)"]
    with registry.activate(session), pytest.raises(RuntimeError, match="active compaction phase"):
        await registry.invalidate_view("rebuilt")


async def test_a_pass_resuming_its_own_layout_still_conflicts_with_a_view_published_meanwhile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="reuse")
    await _pass(_registry(budget, [], view_store=store), session, _Rebuilding(transcript).hand())
    slow, fast = _registry(budget, [], view_store=store), _registry(budget, [], view_store=store)

    with slow.activate(session) as stack:
        strategy = cast(_Composed, stack.strategy)
        # A layout of its own is pending, so the stored view is not loaded; the versions this
        # pass will write over must still be observed now.
        earlier = await store.read(ViewKey("reuse", "agent", ViewPart.EARLIER))
        current = await store.read(ViewKey("reuse", "agent", ViewPart.CURRENT))
        assert earlier is not None and current is not None
        offer = strategy.persisted_view_offer(earlier.data["layout"], current.data["layout"])
        assert offer is not None
        strategy.restore_saved_view(offer)
        await slow._load_view("reuse", strategy)
        await _pass(fast, AgentSession(session_id="reuse"), _Rebuilding(transcript).hand())
        published = await store.read(ViewKey("reuse", "agent", ViewPart.CURRENT))
        await apply_compaction(_Rebuilding(transcript).hand(), strategy=_compactor(strategy))
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await slow._persist_view("reuse", strategy)

    kept = await store.read(ViewKey("reuse", "agent", ViewPart.CURRENT))
    assert published is not None and kept is not None and kept.etag == published.etag
    assert any("another writer's is newer" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "reconfigure",
    [
        pytest.param(
            lambda s: setattr(s.tool_results.fallback, "keep_tail_groups", 99), id="fallback"
        ),
        pytest.param(lambda s: s.user_turns.summary_options.update(temperature=0.9), id="options"),
        pytest.param(
            lambda s: s.user_turns.summary_options.update(stop=["new"]), id="nested-options"
        ),
    ],
)
async def test_a_view_written_under_other_fallback_or_summary_options_is_not_offered(
    reconfigure: Any, caplog: pytest.LogCaptureFixture
) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="nested")
    await _pass(_registry(budget, [], view_store=store), session, _Rebuilding(transcript).hand())
    reconfigured = _registry(budget, [], view_store=store)

    with reconfigured.activate(session) as stack:
        reconfiguring = cast(_Composed, stack.strategy)
        reconfigure(reconfiguring)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await reconfigured._load_view("nested", reconfiguring)
        assert not reconfiguring.has_saved_view

    assert any("other decisions or settings" in r.getMessage() for r in caplog.records)


async def test_a_session_moving_between_processes_does_not_rewrite_an_unchanged_earlier_part(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = _CountingStore()
    session = AgentSession(session_id="handoff")
    host = _Rebuilding(transcript)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for call in range(1, 5):
            # A fresh process each call: nothing carried, the view comes from the store.
            await _pass(_registry(budget, [], view_store=store), session, host.hand())
            host.answered(call)

    assert "persisted view restored" in _modes(caplog)
    assert store.writes[ViewPart.CURRENT] == 4
    assert store.writes[ViewPart.EARLIER] < store.writes[ViewPart.CURRENT]


async def test_a_pass_whose_pre_pass_read_failed_does_not_write_over_a_view_published_meanwhile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from maf_compaction._session import _guarded_view_io

    class _Flaky(InMemoryViewStore):
        down = True

        async def read(self, key: ViewKey) -> Any:
            if self.down:
                raise OSError("unreachable")
            return await super().read(key)

    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = _Flaky()
    slow, fast = _registry(budget, [], view_store=store), _registry(budget, [], view_store=store)
    session = AgentSession(session_id="flaky")

    with slow.activate(session) as stack:
        strategy = cast(_Composed, stack.strategy)
        await _guarded_view_io(slow._load_view("flaky", strategy))  # the read fails
        store.down = False  # the store recovers, and another process publishes
        await _pass(fast, AgentSession(session_id="flaky"), _Rebuilding(transcript).hand())
        published = await store.read(ViewKey("flaky", "agent", ViewPart.CURRENT))
        await apply_compaction(_Rebuilding(transcript).hand(), strategy=_compactor(strategy))
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await slow._persist_view("flaky", strategy)
        assert not strategy.carries_view

    kept = await store.read(ViewKey("flaky", "agent", ViewPart.CURRENT))
    assert published is not None and kept is not None and kept.etag == published.etag
    assert any("versions were not observed" in r.getMessage() for r in caplog.records)


async def test_invalidating_a_released_session_clears_its_saved_layout() -> None:
    transcript = _transcript(14, 10, 6)
    registry = _registry(
        _budget(transcript, 0.6), [], view_store=InMemoryViewStore(), saved_view_key="saved"
    )
    session = AgentSession(session_id="released")
    await _pass(registry, session, _Rebuilding(transcript).hand())
    registry.release("released")
    assert "saved" in session.state  # release keeps the saved layout with the session

    await registry.invalidate_view(session)

    assert "saved" not in session.state


async def test_invalidation_deletes_the_versions_stored_now_not_those_a_pass_observed() -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="moved-on")
    first = _registry(budget, [], view_store=store)
    await _pass(first, session, _Rebuilding(transcript).hand())
    # Another process publishes newer parts after the first one last saw them.
    await _pass(
        _registry(budget, [], view_store=store),
        AgentSession(session_id="moved-on"),
        _Rebuilding(transcript).hand(),
    )

    await first.invalidate_view("moved-on")

    for part in ViewPart:
        assert await store.read(ViewKey("moved-on", "agent", part)) is None


async def test_a_setting_json_cannot_represent_disables_the_persisted_view() -> None:
    transcript = _transcript(14, 10, 6)
    store = _CountingStore()
    session = AgentSession(session_id="opaque")
    registry = _registry(_budget(transcript, 0.6), [], view_store=store)

    with registry.activate(session) as stack:
        cast(_Composed, stack.strategy).user_turns.summary_options["opaque"] = object()
    await _pass(registry, session, _Rebuilding(transcript).hand())

    assert sum(store.writes.values()) == 0


async def test_a_stored_view_is_parsed_and_joined_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="restored")
    await _pass(_registry(budget, [], view_store=store), session, _Rebuilding(transcript).hand())
    loop_thread = threading.get_ident()
    threads: list[int] = []
    original = _Composed.persisted_view_offer

    def recorded(self: _Composed, earlier: str, current: str) -> Any:
        threads.append(threading.get_ident())
        return original(self, earlier, current)

    monkeypatch.setattr(_Composed, "persisted_view_offer", recorded)
    fresh = _registry(budget, [], view_store=store)
    with fresh.activate(session) as stack:
        strategy = cast(_Composed, stack.strategy)
        await fresh._load_view("restored", strategy)
        assert strategy.has_saved_view

    assert threads and all(thread != loop_thread for thread in threads)


# Module-level lambdas all share the qualified name ``<lambda>``: only their code differs.
_REWRITERS = (
    lambda attempt: f"Rewrite tighter, attempt {attempt}.",
    lambda attempt: f"Rewrite tighter, attempt {attempt}.",
    lambda attempt: f"Rewrite looser, attempt {attempt}.",
)


@pytest.mark.parametrize(
    ("resuming", "offered"),
    [
        pytest.param(_REWRITERS[1], True, id="same-code"),
        pytest.param(_REWRITERS[2], False, id="other-code"),
    ],
)
async def test_a_callable_setting_is_bound_by_its_code_not_only_its_name(
    resuming: Any, offered: bool
) -> None:
    assert resuming.__qualname__ == _REWRITERS[0].__qualname__
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    store = InMemoryViewStore()
    session = AgentSession(session_id="callable")
    writer = _registry(budget, [], view_store=store)
    with writer.activate(session) as stack:
        cast(_Composed, stack.strategy).rewrite_prompt = _REWRITERS[0]
    await _pass(writer, session, _Rebuilding(transcript).hand())

    reader = _registry(budget, [], view_store=store)
    with reader.activate(session) as stack:
        strategy = cast(_Composed, stack.strategy)
        strategy.rewrite_prompt = resuming
        await reader._load_view("callable", strategy)
        assert strategy.has_saved_view is offered
