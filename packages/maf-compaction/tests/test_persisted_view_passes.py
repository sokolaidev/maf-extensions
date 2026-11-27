"""Passes persist the view through a store and another process resumes from it (#2995)."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

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
            strategy: _Composed = stack.strategy
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

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        sent = await _pass_guarded(registry, session, _Rebuilding(transcript).hand())

    assert sent
    assert sum("view store failed (OSError)" in r.getMessage() for r in caplog.records) == 2


async def _pass_guarded(registry: Any, session: AgentSession, messages: list[Message]) -> list[Any]:
    """:func:`_pass` through the guard the session middleware wraps view I/O in."""
    from maf_compaction._session import _guarded_view_io

    async def compact(messages: list[Message]) -> bool:
        with registry.activate(session) as stack:
            strategy: _Composed = stack.strategy
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
