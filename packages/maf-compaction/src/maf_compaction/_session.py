"""Session-owned compaction stacks for agents shared by many conversations."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
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

from ._composed import (
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _RecordMergeWork,  # pyright: ignore[reportPrivateUsage]
)
from ._offload import off_loop
from ._toolsummary import (
    DEFAULT_RECORD_TARGET_TOKENS,
    RecallGate,
    RecordDecisions,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    make_recall_tool,
)

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


class SessionCompactionRegistry:
    """Cache stacks while treating ``session.state[state_key]`` as authoritative.

    ``build`` must return fresh, identically configured components. Serialize requests for
    each session at the host, including across replicas; distinct sessions may run concurrently.

    A composed strategy keeps the list its last per-call pass left, so the next call need not
    put every decision back. That list holds a conversation's messages, so only the
    ``max_carried_views`` most recently used sessions keep one; zero keeps none.
    Set ``saved_view_key`` to a separate session-state key to persist a layout at turn end.
    """

    def __init__(
        self,
        build: Callable[[], CompactionStack],
        *,
        state_key: str = "maf_compaction",
        max_cached_sessions: int = 128,
        max_carried_views: int = DEFAULT_MAX_CARRIED_VIEWS,
        saved_view_key: str | None = None,
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
        self._discard_merge_work(session_id)

    def _discard_merge_work(self, session_id: str) -> None:
        if retained := self._retained.pop(session_id, None):
            retained.cancel()
        if entry := self._merge_work.get(session_id):
            entry[1].cancel()

    def release_carried_view(self, session_id: str) -> None:
        """Release a turn's kept list, saving its layout when a saved_view_key is configured."""
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
                        key = self.registry.saved_view_key
                        if key is not None and (saved := session.state.pop(key, None)) is not None:
                            stack.strategy.restore_saved_view(saved)
                        return await stack.strategy.compact_before(
                            messages, options=context.options or {}
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
