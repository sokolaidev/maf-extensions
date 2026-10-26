"""Run benchmark requests across memory, serialization and agent reconstruction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

import maf_compaction
from agent_framework import Agent, AgentSession, HistoryProvider, Message

from ._behavior import restore_behavior, snapshot_behavior
from ._configuration import CompactionOptions, validate_session_support
from ._diagnostics import OBSERVATION_LABELS, counters, summary_measurements
from ._tokenizers import REASONING_TOKENS_KEY

_REPLAY_MESSAGES: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "cachebench_replay_messages", default=None
)


def capture_original_messages(messages: Sequence[Message]) -> None:
    """Detach each identified message before compaction can mutate its payload."""
    originals = _REPLAY_MESSAGES.get()
    if originals is not None:
        for message in messages:
            if message.message_id and message.message_id not in originals:
                originals[message.message_id] = deepcopy(message.to_dict())


def update_original_reasoning(messages: Sequence[Message]) -> None:
    """Copy late usage stamps without overwriting original payloads or compaction metadata."""
    originals = _REPLAY_MESSAGES.get()
    if originals is None:
        return
    for message in messages:
        original = originals.get(message.message_id or "")
        if original is None:
            continue
        for content in message.contents:
            stamp = content.additional_properties.get(REASONING_TOKENS_KEY)
            if content.protected_data is None or not isinstance(stamp, int) or stamp <= 0:
                continue
            for part in original["contents"]:
                if (
                    part.get("id") == content.id
                    and part.get("protected_data") == content.protected_data
                ):
                    part.setdefault("additional_properties", {})[REASONING_TOKENS_KEY] = stamp
                    break


@dataclass
class LiveBinding:
    """One agent and its exclusive mutable compaction objects."""

    agent: Agent[Any]
    strategy: Any
    recall: maf_compaction.ToolResultRecallMiddleware | None
    gate: maf_compaction.RecallGate | None

    def stack(self) -> maf_compaction.CompactionStack:
        """Return the native persistence stack for a record-bearing strategy."""
        if self.recall is None or self.gate is None:
            raise ValueError("This strategy has no recall stack.")
        return maf_compaction.CompactionStack(self.strategy, self.gate, self.recall)


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
        self.observations = {name: 0 for name in OBSERVATION_LABELS}
        self.registry = self._registry()
        validate_session_support(options, has_recall=self.binding.recall is not None)

    def _registry(self) -> maf_compaction.SessionCompactionRegistry:
        def build() -> maf_compaction.CompactionStack:
            self.binding = self.build()
            return self.binding.stack()

        return maf_compaction.SessionCompactionRegistry(build, max_cached_sessions=1)

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

        captured = {item["message_id"]: deepcopy(item) for item in original}

        def remember(active: AgentSession) -> None:
            user_strategy = maf_compaction.find_nested_strategy(
                self.binding.strategy,
                maf_compaction.UserTurnAnchoredSummarizationCompactionStrategy,
            )
            for provider in self.binding.agent.context_providers:
                if isinstance(provider, HistoryProvider):
                    saved = active.state.get(provider.source_id, {}).get("messages", [])
                    update_original_reasoning(saved)
                    if user_strategy is not None:
                        self.observations.update(
                            summary_measurements(saved, user_strategy.tokenizer)
                        )
                    break
            if self.options.history_mode == "replay":
                active.state[replay_key] = list(captured.values())

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

        token = _REPLAY_MESSAGES.set(captured if self.options.history_mode == "replay" else None)
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
            _REPLAY_MESSAGES.reset(token)
            if before is not None:
                after = counters(self.binding.strategy, self.binding.recall)
                for name, value in after.items():
                    if name == "records_found":
                        self.totals[name] = max(self.totals[name], value)
                    else:
                        self.totals[name] += max(0, value - before[name])
                records = getattr(self.binding.strategy, "records_in_conversation", 0)
                if isinstance(records, int):
                    self.observations["records_in_conversation"] = max(
                        self.observations["records_in_conversation"], records
                    )
                value = getattr(self.binding.strategy, "user_messages_replaced", 0)
                if isinstance(value, int) and value > 0:
                    self.observations["user_messages_replaced"] = value


def _json_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(data, allow_nan=False)))
