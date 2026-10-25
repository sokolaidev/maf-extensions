"""Run benchmark requests across memory, serialization and agent reconstruction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import maf_compaction
from agent_framework import Agent, AgentSession, HistoryProvider, Message
from maf_compaction import (
    CompactionStack,
    RecallGate,
    SessionCompactionRegistry,
    ToolResultRecallMiddleware,
)

from ._behavior import restore_behavior, snapshot_behavior
from ._configuration import CompactionOptions, validate_session_support
from ._diagnostics import counters


@dataclass
class LiveBinding:
    """One agent and its exclusive mutable compaction objects."""

    agent: Agent[Any]
    strategy: Any
    recall: ToolResultRecallMiddleware | None
    gate: RecallGate | None

    def stack(self) -> CompactionStack:
        """Return the native persistence stack for a record-bearing strategy."""
        if self.recall is None or self.gate is None:
            raise ValueError("This strategy has no recall stack.")
        return CompactionStack(self.strategy, self.gate, self.recall)


class SessionExperiment:
    """Serialize complete requests using detached snapshots and an exclusive local lease.

    The store is in memory: this measures state restoration, not storage latency or durability.
    """

    def __init__(self, build: Callable[[], LiveBinding], options: CompactionOptions) -> None:
        self.build = build
        self.options = options
        self.binding = build()
        self.lock = asyncio.Lock()
        self.totals = counters(self.binding.strategy, self.binding.recall)
        self.registry = self._registry()
        validate_session_support(options, has_recall=self.binding.recall is not None)

    def _registry(self) -> SessionCompactionRegistry:
        def build() -> CompactionStack:
            self.binding = self.build()
            return self.binding.stack()

        return SessionCompactionRegistry(build, max_cached_sessions=1)

    async def run(
        self, session: AgentSession, text: str, options: dict[str, Any]
    ) -> tuple[Any, AgentSession]:
        """Execute one complete request and return its accepted session."""
        inputs: Any = text
        replay_key = "cachebench_original_history"
        original = [
            item.to_dict() if isinstance(item, Message) else item
            for item in session.state.get(replay_key, [])
        ]
        user = Message("user", [text], message_id=f"cachebench_user_{len(original)}")
        if self.options.history_mode == "replay":
            session = AgentSession.from_dict(_json_snapshot(session.to_dict()))
            for provider in self.binding.agent.context_providers:
                if isinstance(provider, HistoryProvider):
                    session.state.pop(provider.source_id, None)
            inputs = [*(Message.from_dict(item) for item in original), user]

        before: dict[str, int] | None = None

        def remember(active: AgentSession) -> None:
            if self.options.history_mode != "replay":
                return
            # Stream aggregation can return different message objects from persisted history.
            saved: list[Message] = []
            for provider in self.binding.agent.context_providers:
                if isinstance(provider, HistoryProvider):
                    saved = active.state.get(provider.source_id, {}).get("messages", [])
                    break
            boundary = next(
                index
                for index, message in enumerate(saved)
                if message.message_id == user.message_id
            )
            active.state[replay_key] = [
                *original,
                user.to_dict(),
                *(message.to_dict() for message in saved[boundary + 1 :]),
            ]

        async def call(active: AgentSession) -> Any:
            nonlocal before
            before = counters(self.binding.strategy, self.binding.recall)
            if self.options.stream:
                response = self.binding.agent.run(
                    inputs, session=active, options=options, stream=True
                )
                try:
                    result = await response.get_final_response()
                finally:
                    await response.close()
            else:
                result = await self.binding.agent.run(inputs, session=active, options=options)
            remember(active)
            return result

        try:
            mode = self.options.session_mode
            if mode == "memory":
                result = await call(session)
            elif self.binding.recall is None:
                behavioral = snapshot_behavior(self.binding.strategy, None)
                session = AgentSession.from_dict(_json_snapshot(session.to_dict()))
                if mode == "restart":
                    self.binding = self.build()
                restore_behavior(self.binding.strategy, None, behavioral)
                result = await call(session)
                session = AgentSession.from_dict(_json_snapshot(session.to_dict()))
            else:
                session.state[self.registry.state_key] = self.binding.stack().to_dict()
                data = _json_snapshot(session.to_dict())
                if mode == "restart":
                    self.registry = self._registry()

                async def load(_: str) -> dict[str, Any]:
                    return _json_snapshot(data)

                async def save(_: str, snapshot: dict[str, Any]) -> None:
                    nonlocal data
                    data = _json_snapshot(snapshot)

                @asynccontextmanager
                async def lease(_: str) -> AsyncGenerator[None]:
                    async with self.lock:
                        yield

                persistence_type: Any = getattr(maf_compaction, "SessionPersistence")
                persistence = persistence_type(self.registry, load=load, save=save, lease=lease)

                async def complete(active: AgentSession) -> Any:
                    with self.registry.activate(active):
                        return await call(active)

                if self.options.stream:
                    active_session: AgentSession | None = None

                    def stream(active: AgentSession) -> Any:
                        nonlocal before, active_session
                        active_session = active
                        before = counters(self.binding.strategy, self.binding.recall)
                        return self.binding.agent.run(
                            inputs, session=active, options=options, stream=True
                        ).with_consumption_context_manager(lambda: self.registry.activate(active))

                    async with persistence.stream(session.session_id, stream) as response:
                        result = await response.get_final_response()
                        assert active_session is not None
                        remember(active_session)
                else:
                    result = await persistence.run(session.session_id, complete)
                session = AgentSession.from_dict(data)
            return result, session
        finally:
            if before is not None:
                after = counters(self.binding.strategy, self.binding.recall)
                for name, value in after.items():
                    self.totals[name] = self.totals.get(name, 0) + max(0, value - before[name])


def _json_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(data, allow_nan=False)))
