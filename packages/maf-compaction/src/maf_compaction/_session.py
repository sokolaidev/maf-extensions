"""Session-owned compaction stacks for agents shared by many conversations."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agent_framework import (
    AgentContext,
    AgentMiddleware,
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
from ._toolsummary import (
    DEFAULT_RECORD_TARGET_TOKENS,
    RecallGate,
    RecordDecisions,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    make_recall_tool,
)

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
    session: AgentSession
    stack: CompactionStack


class SessionCompactionRegistry:
    """Cache stacks while treating ``session.state[state_key]`` as authoritative.

    ``build`` must return fresh, identically configured components. Serialize requests for
    each session at the host, including across replicas; distinct sessions may run concurrently.
    """

    def __init__(
        self,
        build: Callable[[], CompactionStack],
        *,
        state_key: str = "maf_compaction",
        max_cached_sessions: int = 128,
    ) -> None:
        if max_cached_sessions < 0:
            raise ValueError("max_cached_sessions must be non-negative.")
        if not state_key:
            raise ValueError("state_key must not be empty.")
        self.build = build
        self.state_key = state_key
        self.max_cached_sessions = max_cached_sessions
        self._cache: OrderedDict[str, CompactionStack] = OrderedDict()
        self._active: dict[str, _Binding] = {}
        self._current: ContextVar[_Binding | None] = ContextVar("compaction_session", default=None)
        self._merge_work: dict[str, tuple[AgentSession, _RecordMergeWork]] = {}

    @contextmanager
    def activate(self, session: AgentSession) -> Generator[CompactionStack]:
        """Restore a stack for one phase and persist it even when the phase raises."""
        current = self._current.get()
        if current is not None and current.session is session:
            if self._active.get(session.session_id) is current:
                try:
                    yield current.stack
                finally:
                    session.state[self.state_key] = current.stack.to_dict()
                    self._remember_merge_state(session)
                return
        if session.session_id in self._active:
            raise RuntimeError("Overlapping compaction phases for one session are unsupported.")
        stack = self._cache.pop(session.session_id, None)
        data = session.state.get(self.state_key)
        # A removed snapshot is a reset, even when this session is still cached.
        if stack is None or data is None:
            stack = self.build()
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
                del self._active[session.session_id]
            self._cache[session.session_id] = stack
            while len(self._cache) > self.max_cached_sessions:
                self._cache.popitem(last=False)

    def _remember_merge_state(self, session: AgentSession) -> None:
        if entry := self._merge_work.get(session.session_id):
            entry[1].expected_state = deepcopy(session.state[self.state_key])

    def current_stack(self) -> CompactionStack:
        """Return the active stack, refusing calls outside a session phase."""
        binding = self._current.get()
        if binding is None or self._active.get(binding.session.session_id) is not binding:
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


class _RecordMergeRunMiddleware(AgentMiddleware):
    def __init__(self, registry: SessionCompactionRegistry) -> None:
        self.registry = registry

    async def process(
        self, context: AgentContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        session = context.session
        if session is None:
            raise RuntimeError("Background record merging requires an AgentSession.")
        if session.session_id in self.registry._merge_work:  # pyright: ignore[reportPrivateUsage]
            raise RuntimeError("Overlapping agent runs for one compaction session are unsupported.")
        work = _RecordMergeWork()
        entry = session, work
        self.registry._merge_work[session.session_id] = entry  # pyright: ignore[reportPrivateUsage]

        async def close() -> None:
            if self.registry._merge_work.get(session.session_id) is entry:  # pyright: ignore[reportPrivateUsage]
                del self.registry._merge_work[session.session_id]  # pyright: ignore[reportPrivateUsage]
            await work.close()

        if context.stream:
            context.stream_cleanup_hooks.append(close)
        try:
            await call_next()
        except BaseException:
            await close()
            raise
        if not isinstance(context.result, ResponseStream):
            await close()


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

        def observed(response: ChatResponse) -> ChatResponse:
            work.start(list(response.messages))
            return response

        await call_next()
        if isinstance(context.result, ResponseStream):
            context.result.with_result_hook(observed)
        elif isinstance(context.result, ChatResponse):
            observed(context.result)


def make_session_record_merge_middleware(registry: SessionCompactionRegistry) -> MiddlewareBundle:
    """Prepare record merges during tools, with cancellation scoped to one agent run.

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
