"""Session-owned compaction stacks for agents shared by many conversations."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final
from weakref import WeakValueDictionary

from agent_framework import (
    AgentContext,
    AgentMiddleware,
    AgentResponseUpdate,
    AgentSession,
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    CompactionProvider,
    FunctionInvocationContext,
    Message,
    MiddlewareBundle,
    ResponseStream,
    TokenizerProtocol,
)

from ._carryover import _is_transient  # pyright: ignore[reportPrivateUsage]
from ._composed import (
    PASS_MODE_PERSISTED,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _RecordMergeWork,  # pyright: ignore[reportPrivateUsage]
)
from ._offload import off_loop
from ._persisted_view import (
    CompactedViewStore,
    PersistedViewPart,
    ViewKey,
    ViewPart,
    ViewWriteConflict,
)
from ._toolsummary import (
    DEFAULT_RECORD_TARGET_TOKENS,
    RecallGate,
    RecordDecisions,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    make_recall_tool,
)
from ._view_digest import layout_text_digest

#: Sessions whose strategy may keep the list its last per-call pass left.
DEFAULT_MAX_CARRIED_VIEWS: Final[int] = 16

_Strategy = (
    ToolResultAnchoredSummarizationCompactionStrategy
    | ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
)


@dataclass
class CompactionStack:
    """A fresh strategy, gate and recall middleware wired exclusively to each other."""

    strategy: _Strategy
    gate: RecallGate
    middleware: ToolResultRecallMiddleware

    def __post_init__(self) -> None:
        records = (
            self.strategy.tool_results
            if isinstance(
                self.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
            )
            else self.strategy
        )
        records.bind_recall(self.middleware)

    def to_dict(self) -> dict[str, Any]:
        """Snapshot behavioral state without configuration or diagnostic-only counts."""
        composed = isinstance(
            self.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        strategy = (
            self.strategy.session_state()
            if isinstance(
                self.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
            )
            else self.strategy.decision_state().to_dict()
        )
        return {
            "version": 9,
            "kind": "composed" if composed else "record",
            "strategy": strategy,
            "gate": self.gate.session_state(),
            "middleware": self.middleware.session_state(),
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Restore compatible state; legacy recall identities require an explicit rebuild."""
        composed = isinstance(
            self.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        )
        if data.get("version") not in (1, 2, 3, 4, 5, 6, 7, 8, 9) or data.get("kind") != (
            "composed" if composed else "record"
        ):
            raise ValueError("Unsupported compaction state version or stack kind.")
        if isinstance(self.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy):
            self.strategy.restore_session_state(data["strategy"])
        else:
            self.strategy.restore_decisions(RecordDecisions.from_dict(data["strategy"]))
        self.gate.restore_session_state(data["gate"])
        self.middleware.restore_session_state(data["middleware"])


@dataclass
class _Binding:
    session: AgentSession | None
    stack: CompactionStack | None


logger = logging.getLogger(__name__)

#: The agent key of a registry's views when the host names none.
DEFAULT_VIEW_AGENT: Final[str] = "agent"
#: Reads and deletes an invalidation tries before a part that keeps changing raises.
_INVALIDATE_ATTEMPTS: Final[int] = 3


class SessionCompactionRegistry:
    """Cache stacks while treating ``session.state[state_key]`` as authoritative.

    ``build`` must return fresh, identically configured components. Serialize requests for
    each session at the host, including across replicas; distinct sessions may run concurrently.

    A composed strategy keeps the list its last per-call pass left, so the next call need not
    put every decision back. That list holds a conversation's messages, so only the
    ``max_carried_views`` most recently used sessions keep one; zero keeps none.
    Set ``saved_view_key`` to a separate session-state key to persist a layout at turn end.
    Set ``view_store`` to persist the view after every per-call pass instead, in an
    earlier-turns part and a current-turn part, and to resume a session from it when this
    process holds no carried view of it (#2995). ``view_agent`` keys this registry's views
    apart from other agents' views of the same conversation. The store must be trusted
    storage, as session state is: see :class:`CompactedViewStore`.
    """

    def __init__(
        self,
        build: Callable[[], CompactionStack],
        *,
        state_key: str = "maf_compaction",
        max_cached_sessions: int = 128,
        max_carried_views: int = DEFAULT_MAX_CARRIED_VIEWS,
        saved_view_key: str | None = None,
        view_store: CompactedViewStore | None = None,
        view_agent: str = DEFAULT_VIEW_AGENT,
    ) -> None:
        if max_cached_sessions < 0:
            raise ValueError("max_cached_sessions must be non-negative.")
        if max_carried_views < 0:
            raise ValueError("max_carried_views must be non-negative.")
        if not state_key:
            raise ValueError("state_key must not be empty.")
        if saved_view_key is not None and (not saved_view_key or saved_view_key == state_key):
            raise ValueError("saved_view_key must be nonempty and differ from state_key.")
        self.build = build
        self.state_key = state_key
        self.max_cached_sessions = max_cached_sessions
        self.max_carried_views = max_carried_views
        self.saved_view_key = saved_view_key
        self.view_store = view_store
        self.view_agent = view_agent
        # What this process knows of each session's stored view; dropped with the session.
        self._views: dict[str, _ViewState] = {}
        # Per session, how many sources the running turn's first pass was handed: what the
        # host has stored. Dropped with the turn's carried view, so bounded by running turns.
        self._handed: dict[str, int] = {}
        self._owners: WeakValueDictionary[str, AgentSession] = WeakValueDictionary()
        self._cache: OrderedDict[str, CompactionStack] = OrderedDict()
        self._active: dict[str, _Binding] = {}
        self._current: ContextVar[_Binding | None] = ContextVar("compaction_session", default=None)
        self._merge_work: dict[str, tuple[AgentSession, _RecordMergeWork]] = {}
        self._retained: dict[str, _RecordMergeWork] = {}

    @contextmanager
    def activate(self, session: AgentSession) -> Generator[CompactionStack]:
        """Restore a stack for one phase and persist it even when the phase raises."""
        current = self._current.get()
        if current is not None and current.session is session:
            if self._active.get(session.session_id) is current and current.stack is not None:
                stack = current.stack
                try:
                    yield stack
                finally:
                    session.state[self.state_key] = stack.to_dict()
                    self._remember_merge_state(session)
                return
        if session.session_id in self._active:
            raise RuntimeError("Overlapping compaction phases for one session are unsupported.")
        stack = self._cache.pop(session.session_id, None)
        data = session.state.get(self.state_key)
        if retained := self._retained.get(session.session_id):
            if stack is None or retained.expected_state != data:
                self._retained.pop(session.session_id).cancel()
        # A removed snapshot is a reset, even when this session is still cached.
        if stack is None or data is None:
            stack = self.build()
        if data is None and self.saved_view_key is not None:
            session.state.pop(self.saved_view_key, None)
        if entry := self._merge_work.get(session.session_id):
            owner, work = entry
            if owner is not session:
                raise RuntimeError("Background record work belongs to another session object.")
            if not isinstance(
                stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
            ):
                raise ValueError("Background record merging requires a composed strategy.")
            work.bind(stack.strategy, data)
        if data is not None:
            stack.restore(data)
        stack.middleware._bind_session_id(session.session_id)  # pyright: ignore[reportPrivateUsage]
        binding = _Binding(session, stack)
        self._active[session.session_id] = binding
        token = self._current.set(binding)
        try:
            yield stack
        finally:
            try:
                session.state[self.state_key] = stack.to_dict()
                self._remember_merge_state(session)
            finally:
                self._current.reset(token)
                # Copied contexts may outlive the phase, including cancelled timer contexts.
                binding.session = None
                binding.stack = None
                del self._active[session.session_id]
            self._cache[session.session_id] = stack
            if self.saved_view_key is not None:
                self._owners[session.session_id] = session
            while len(self._cache) > self.max_cached_sessions:
                evicted, _ = self._cache.popitem(last=False)
                self._owners.pop(evicted, None)
                self._views.pop(evicted, None)
                # Not the turn's handed count: a turn can go on after its stack is evicted, and
                # a later pass would count the run's own messages as handed. It goes when the
                # turn ends, which releases the view whether the stack is cached or not.
                self._discard_merge_work(evicted)
            self._release_carried_views()

    def _release_carried_views(self) -> None:
        carried = 0
        for stack in reversed(self._cache.values()):
            strategy = stack.strategy
            if (
                isinstance(strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy)
                and strategy.carries_view
            ):
                carried += 1
                if carried > self.max_carried_views:
                    strategy.forget_carried_view()

    def release(self, session_id: str) -> None:
        """Drop a session's cached stack and carried view, preserving its saved state.

        An uncached session needs no cleanup. An active phase raises RuntimeError.
        """
        if session_id in self._active:
            raise RuntimeError("Cannot release a session during an active compaction phase.")
        self.release_carried_view(session_id)
        self._cache.pop(session_id, None)
        self._owners.pop(session_id, None)
        self._views.pop(session_id, None)
        self._handed.pop(session_id, None)
        self._discard_merge_work(session_id)

    def _discard_merge_work(self, session_id: str) -> None:
        if retained := self._retained.pop(session_id, None):
            retained.cancel()
        if entry := self._merge_work.get(session_id):
            entry[1].cancel()

    def release_carried_view(self, session_id: str) -> None:
        """Release a turn's kept list, saving its layout when a saved_view_key is configured.

        The turn's count of handed messages goes too: the next pass starts a turn of its own.
        """
        self._handed.pop(session_id, None)
        stack = self._cache.get(session_id)
        if stack is None or not isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        ):
            return
        owner = self._owners.get(session_id)
        if self.saved_view_key is not None and owner is not None and stack.strategy.carries_view:
            saved = stack.strategy.take_saved_view()
            if saved is not None:
                owner.state[self.saved_view_key] = saved
            else:
                owner.state.pop(self.saved_view_key, None)
        else:
            stack.strategy.forget_carried_view()

    async def _end_turn(self, session_id: str) -> None:
        if self.saved_view_key is None:
            self.release_carried_view(session_id)
        else:
            await off_loop(self.release_carried_view, session_id)

    def _view_key(self, session_id: str, part: ViewPart) -> ViewKey:
        return ViewKey(session_id, self.view_agent, part)

    async def _observe(self, session_id: str) -> None:
        """Record each stored part's version, absence included, as what this pass writes over."""
        assert self.view_store is not None
        etags: dict[ViewPart, str | None] = {}
        for part in ViewPart:
            stored = await self.view_store.read(self._view_key(session_id, part))
            etags[part] = None if stored is None else stored.etag
        self._views.setdefault(session_id, _ViewState()).etags = etags

    async def _load_view(
        self,
        session_id: str,
        strategy: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ) -> None:
        """Offer the stored view to a strategy that holds no view of its own (#2995).

        Either way, the versions this pass will write over are observed before it runs.
        """
        store = self.view_store
        if store is None:
            return
        if strategy.carries_view or strategy.has_saved_view:
            known = self._views.get(session_id)
            if known is None or any(part not in known.etags for part in ViewPart):
                await self._observe(session_id)
            return
        earlier = await store.read(self._view_key(session_id, ViewPart.EARLIER))
        current = await store.read(self._view_key(session_id, ViewPart.CURRENT))
        # What this pass will write over, absence included, observed before it runs.
        self._views[session_id] = _ViewState(
            etags={
                ViewPart.EARLIER: None if earlier is None else earlier.etag,
                ViewPart.CURRENT: None if current is None else current.etag,
            }
        )
        if earlier is None or current is None:
            reason = "none stored" if earlier is None and current is None else "a part is missing"
            logger.info("Compaction persisted view not offered: %s.", reason)
            return
        # Parsing, hashing and joining the stored layout is history-wide: off the loop.
        accepted = await off_loop(partial(_accepted_view, strategy, earlier.data, current.data))
        if isinstance(accepted, str):
            logger.info("Compaction persisted view not offered: %s.", accepted)
            return
        offer, earlier_digest, earlier_sources = accepted
        strategy.restore_saved_view(offer, source=PASS_MODE_PERSISTED)
        # The stored earlier part is this process's now, so an unchanged one is not rewritten.
        state = self._views[session_id]
        state.earlier_digest = earlier_digest
        state.earlier_sources = earlier_sources

    def _note_handed(
        self,
        session_id: str,
        strategy: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
        handed: list[Message],
    ) -> int:
        """How many sources the running turn was handed, set by its first pass.

        The first pass's sources are all messages the host handed it; a later pass's sources go
        on with the messages the run itself produced. ``handed`` is the list this pass was
        handed, counted when the pass kept no view to count from.
        """
        if session_id not in self._handed:
            counted = strategy.carried_source_count
            self._handed[session_id] = (
                counted if counted is not None else sum(not _is_transient(m) for m in handed)
            )
        return self._handed[session_id]

    async def _persist_view(
        self,
        session_id: str,
        strategy: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
        *,
        handed_sources: int | None = None,
    ) -> None:
        """Write the view the pass left: the earlier part only when it changed (#2995).

        ``handed_sources`` keeps the view to what the host handed the turn
        (:meth:`~._composed.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy.persisted_view_parts`).
        """
        store = self.view_store
        if store is None:
            return
        state = self._views.get(session_id)
        if state is None or any(part not in state.etags for part in ViewPart):
            # The versions this pass would write over were not observed before it ran (a store
            # read failed), so a write could overwrite a view published meanwhile. This
            # process's view may be the older one: drop it, and the next pass reads first.
            strategy.forget_carried_view()
            self._views.pop(session_id, None)
            logger.info(
                "Compaction persisted view not written: the stored versions were not observed "
                "before the pass."
            )
            return
        # History-wide work: off the loop, as the pass's own steps are.
        view = await off_loop(
            partial(
                strategy.persisted_view_parts,
                unchanged_earlier=state.earlier_digest,
                handed_sources=handed_sources,
            )
        )
        if view is None:
            return
        earlier, earlier_digest, current, binding = view
        parts: list[PersistedViewPart] = []
        if earlier is None and state.earlier_sources is not None:
            earlier_sources = state.earlier_sources
        elif earlier is None:
            return
        else:
            earlier_sources = _source_count(earlier)
            parts.append(
                PersistedViewPart(ViewPart.EARLIER, 0, earlier_sources, earlier, earlier_digest)
            )
        end = earlier_sources + _source_count(current)
        parts.append(PersistedViewPart(ViewPart.CURRENT, earlier_sources, end, current, binding))
        for part in parts:
            key = self._view_key(session_id, part.part)
            try:
                etag = await store.write(key, part.to_dict(), etag=state.etags[part.part])
            except ViewWriteConflict:
                # Another writer published a newer view. This process's view is now stale: it
                # must not be written again, so drop it and what was observed, and the next
                # pass reads the newer view or rebuilds.
                strategy.forget_carried_view()
                self._views.pop(session_id, None)
                logger.info("Compaction persisted view not written: another writer's is newer.")
                return
            state.etags[part.part] = etag
            if part.part is ViewPart.EARLIER:
                state.earlier_digest = earlier_digest
                state.earlier_sources = earlier_sources

    async def invalidate_view(self, session: AgentSession | str) -> None:
        """Remove a session's stored view, so its next pass starts from the stored history.

        For a host event that makes the view wrong: a branch, a rollback, a deleted or
        relabelled stored message. The view this process carries for the session, and a
        layout waiting under ``saved_view_key``, go too, so nothing resumes or writes the
        invalidated view again. Pass the session itself when it may have been released or
        evicted: its saved layout lives in its state, which a session id alone cannot reach.
        Each part is deleted at the version stored now, read afresh. Raises RuntimeError during
        the session's active phase, as :meth:`release` does, and ViewWriteConflict if a part
        keeps changing while it is deleted.
        """
        session_id = session if isinstance(session, str) else session.session_id
        if session_id in self._active:
            raise RuntimeError("Cannot invalidate a view during an active compaction phase.")
        stack = self._cache.get(session_id)
        if stack is not None and isinstance(
            stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
        ):
            stack.strategy.forget_carried_view()
        if self.saved_view_key is not None:
            for owner in (self._owners.get(session_id), session):
                if isinstance(owner, AgentSession):
                    owner.state.pop(self.saved_view_key, None)
        store = self.view_store
        if store is not None:
            for part in ViewPart:
                await _delete_stored(store, self._view_key(session_id, part))
        self._views.pop(session_id, None)
        # The turn's handed count belongs to the history being invalidated.
        self._handed.pop(session_id, None)
        logger.info("Compaction persisted view invalidated by the host.")

    def _remember_merge_state(self, session: AgentSession) -> None:
        if entry := self._merge_work.get(session.session_id):
            entry[1].expected_state = deepcopy(session.state[self.state_key])

    def current_stack(self) -> CompactionStack:
        """Return the active stack, refusing calls outside a session phase."""
        binding = self._current.get()
        if (
            binding is None
            or binding.session is None
            or binding.stack is None
            or self._active.get(binding.session.session_id) is not binding
        ):
            raise RuntimeError("No active compaction session.")
        return binding.stack


class SessionCompactionProvider(CompactionProvider):
    """Compact loaded context and stored history with a session's own stack."""

    def __init__(
        self,
        registry: SessionCompactionRegistry,
        *,
        tokenizer: TokenizerProtocol | None = None,
        source_id: str = "session_compaction",
        history_source_id: str = "in_memory",
        before: bool = True,
        after: bool = True,
    ) -> None:
        if registry.state_key in (source_id, history_source_id):
            raise ValueError("The registry state_key must differ from provider source IDs.")
        if registry.saved_view_key in (source_id, history_source_id):
            raise ValueError("The registry saved_view_key must differ from provider source IDs.")
        self.registry = registry
        super().__init__(
            before_strategy=self._compact if before else None,
            after_strategy=self._compact if after else None,
            tokenizer=tokenizer,
            source_id=source_id,
            history_source_id=history_source_id,
        )

    async def _compact(self, messages: list[Message]) -> bool:
        return await self.registry.current_stack().strategy(messages)

    async def before_run(
        self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]
    ) -> None:
        with self.registry.activate(session):
            await super().before_run(agent=agent, session=session, context=context, state=state)

    async def after_run(
        self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]
    ) -> None:
        with self.registry.activate(session):
            await super().after_run(agent=agent, session=session, context=context, state=state)
        await self.registry._end_turn(session.session_id)  # pyright: ignore[reportPrivateUsage]


class _RecordMergeRunMiddleware(AgentMiddleware):
    def __init__(self, registry: SessionCompactionRegistry) -> None:
        self.registry = registry

    async def process(
        self, context: AgentContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        session = context.session
        if session is None:
            raise RuntimeError("Background record merging requires an AgentSession.")
        workers = self.registry._merge_work  # pyright: ignore[reportPrivateUsage]
        if session.session_id in workers:
            raise RuntimeError("Overlapping agent runs for one compaction session are unsupported.")
        retained = self.registry._retained  # pyright: ignore[reportPrivateUsage]
        work = retained.pop(session.session_id, None) or _RecordMergeWork()
        entry = session, work
        workers[session.session_id] = entry
        close_lock = asyncio.Lock()

        async def close(completed: bool = False) -> None:
            async with close_lock:
                if workers.get(session.session_id) is not entry:
                    return
                try:
                    async with AsyncExitStack() as discard:
                        discard.push_async_callback(work.close)
                        await self.registry._end_turn(session.session_id)  # pyright: ignore[reportPrivateUsage]
                        cached = self.registry._cache.get(session.session_id)  # pyright: ignore[reportPrivateUsage]
                        if (
                            completed
                            and cached
                            and cached.strategy is work.strategy
                            and work.retain()
                        ):
                            retained[session.session_id] = work
                            discard.pop_all()
                finally:
                    workers.pop(session.session_id)

        try:
            await call_next()
        except BaseException:
            await close()
            raise
        if isinstance(stream := context.result, ResponseStream):

            async def updates() -> AsyncIterator[AgentResponseUpdate]:
                async for update in stream:
                    yield update
                await close(completed=True)

            async def stream_closed() -> None:
                try:
                    await stream.close()
                finally:
                    await close()

            context.result = ResponseStream(
                updates(),
                finalizer=lambda _: stream.get_final_response(),
                cleanup_hooks=[stream_closed],
            )
        else:
            await close(completed=True)


def _accepted_view(
    strategy: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    earlier: dict[str, Any],
    current: dict[str, Any],
) -> tuple[dict[str, Any], str, int] | str:
    """The stored parts as a layout this strategy can resume, its earlier part's digest and
    source count, or why the view is not offered."""
    first = PersistedViewPart.from_dict(earlier)
    second = PersistedViewPart.from_dict(current)
    if first is None or second is None:
        return "a part is unreadable"
    earlier_digest = layout_text_digest(first.layout)
    binding = strategy.persisted_view_binding(earlier_digest, second.layout)
    if binding is None or second.config != binding:
        # Decisions or settings changed after the parts were written, or the earlier part is
        # not the one the current part was written with: an interrupted or overtaken write.
        return "it is bound to other decisions or settings"
    offer = strategy.persisted_view_offer(first.layout, second.layout)
    if offer is None:
        return "its parts do not join"
    return offer, earlier_digest, first.end


async def _delete_stored(store: CompactedViewStore, key: ViewKey) -> None:
    """Delete a part at the version stored now, read afresh, never one observed for a pass."""
    for _ in range(_INVALIDATE_ATTEMPTS):
        stored = await store.read(key)
        if stored is None:
            return
        try:
            await store.delete(key, etag=stored.etag)
            return
        except ViewWriteConflict:
            continue  # published between the read and the delete
    raise ViewWriteConflict(f"{key.part} view of {key.agent!r} kept changing while invalidated")


def _source_count(layout: str) -> int:
    return len(json.loads(layout)["sources"])


async def _guarded_view_io(io: Awaitable[None]) -> None:
    """Run a view-store read or write; a store failure is logged and never fails the pass."""
    try:
        await io
    except Exception as error:  # noqa: BLE001 - the view is an optimisation, the pass is not
        logger.warning("Compaction persisted view store failed (%s).", type(error).__name__)


@dataclass
class _ViewState:
    """What one process knows of one session's stored view."""

    #: Per part, the version a write must replace; None records an observed absence.
    etags: dict[ViewPart, str | None] = field(default_factory=lambda: {})
    #: The earlier part this process last wrote, so an unchanged one is not written again.
    earlier_digest: str | None = None
    earlier_sources: int | None = None


class _RecordMergeChatMiddleware(ChatMiddleware):
    def __init__(self, registry: SessionCompactionRegistry) -> None:
        self.registry = registry

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        session = context.session
        if session is None or session.session_id not in self.registry._merge_work:  # pyright: ignore[reportPrivateUsage]
            raise RuntimeError("Background record merging requires its agent-run middleware.")
        owner, work = self.registry._merge_work[session.session_id]  # pyright: ignore[reportPrivateUsage]
        if owner is not session:
            raise RuntimeError("Background record work belongs to another session object.")
        with self.registry.activate(session):
            pass

        async def observed(response: ChatResponse) -> ChatResponse:
            await work.start(list(response.messages))
            return response

        await call_next()
        if isinstance(context.result, ResponseStream):
            context.result.with_result_hook(observed)
        elif isinstance(context.result, ChatResponse):
            await observed(context.result)


def make_session_record_merge_middleware(registry: SessionCompactionRegistry) -> MiddlewareBundle:
    """Prepare record merges during tools and retain late answers on the cached session.

    Install on the agent alongside session compaction and recall middleware. The bundle
    requires explicit sessions and must not be installed on a chat client alone.
    """

    return MiddlewareBundle(
        [_RecordMergeRunMiddleware(registry), _RecordMergeChatMiddleware(registry)]
    )


class SessionToolResultRecallMiddleware(ChatMiddleware):
    """Dispatch recall and optional per-model-call compaction through ``context.session``.

    Enable ``compact_before`` for a harness with per-service-call history loading. The
    strategy captures the session explicitly because a stream may execute after this hook.
    """

    def __init__(
        self, registry: SessionCompactionRegistry, *, compact_before: bool = False
    ) -> None:
        self.registry = registry
        self.compact_before = compact_before

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        session = context.session
        if session is None:
            raise RuntimeError("Session recall middleware requires an AgentSession.")
        if self.compact_before:

            async def compact(messages: list[Message]) -> bool:
                with self.registry.activate(session) as stack:
                    if isinstance(
                        stack.strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
                    ):
                        strategy = stack.strategy
                        key = self.registry.saved_view_key
                        if key is not None and (saved := session.state.pop(key, None)) is not None:
                            strategy.restore_saved_view(saved)
                        await _guarded_view_io(
                            self.registry._load_view(session.session_id, strategy)  # pyright: ignore[reportPrivateUsage]
                        )
                        handed = list(messages)
                        try:
                            return await strategy.compact_before(
                                messages, options=context.options or {}
                            )
                        finally:
                            handed_sources = self.registry._note_handed(  # pyright: ignore[reportPrivateUsage]
                                session.session_id, strategy, handed
                            )
                            await _guarded_view_io(
                                self.registry._persist_view(  # pyright: ignore[reportPrivateUsage]
                                    session.session_id, strategy, handed_sources=handed_sources
                                )
                            )
                    return await stack.strategy(messages)

            context.kwargs = {**context.kwargs, "compaction_strategy": compact}
        with self.registry.activate(session) as stack:
            strategy = stack.strategy
            request_budget = None
            if self.compact_before and isinstance(
                strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
            ):

                def sending_budget() -> int:
                    return strategy.sending_budget(context.options or {})

                request_budget = sending_budget

            await stack.middleware.process(context, call_next, request_budget=request_budget)


def make_session_recall_tool(
    registry: SessionCompactionRegistry,
    *,
    target_tokens: int | None = DEFAULT_RECORD_TARGET_TOKENS,
) -> Callable[..., Awaitable[str]]:
    """Build one registered recall tool whose injected context selects the session gate."""
    description = make_recall_tool(target_tokens=target_tokens)

    async def tool(values: str, ctx: FunctionInvocationContext) -> str:
        if ctx.session is None:
            raise RuntimeError("Session recall tool requires an AgentSession.")
        with registry.activate(ctx.session):
            return make_recall_tool(registry.current_stack().gate, target_tokens=target_tokens)(
                values
            )

    tool.__name__ = description.__name__
    tool.__doc__ = description.__doc__
    return tool
