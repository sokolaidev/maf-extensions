"""Host-owned storage around a complete agent request and its stream lifetime."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from agent_framework import AgentSession, ResponseStream

from ._session import SessionCompactionRegistry


class SessionResponseStream[UpdateT, ResultT](AsyncIterator[UpdateT]):
    """Consume a response inside ``SessionPersistence.stream``; do not retain it after exit.

    Complete iteration or ``get_final_response`` permits saving on a clean context exit.
    Closing early or any consumption failure prevents saving, even if the caller catches it.
    """

    def __init__(self, call: Callable[[], ResponseStream[UpdateT, ResultT]]) -> None:
        self._call = call
        self._stream: ResponseStream[UpdateT, ResultT] | None = None
        self._completed = False
        self._failed = False
        self._closed = False

    @property
    def completed(self) -> bool:
        """Whether final validation succeeded without a later consumption or cleanup failure."""
        return self._completed and not self._failed

    def _response(self) -> ResponseStream[UpdateT, ResultT]:
        if self._stream is None:
            self._stream = self._call()
        return self._stream

    async def __anext__(self) -> UpdateT:
        if self._closed:
            raise StopAsyncIteration
        if self._failed:
            raise RuntimeError("The session response stream failed.")
        try:
            return await self._response().__anext__()
        except StopAsyncIteration:
            await self.get_final_response()
            raise
        except BaseException:
            self._failed = True
            raise

    async def get_final_response(self) -> ResultT:
        """Consume the remaining response and wait for its final validation and provider hooks."""
        if self._closed or self._failed:
            self._failed = True
            raise RuntimeError("The session response stream is closed or failed.")
        try:
            result = await self._response().get_final_response()
        except BaseException:
            self._failed = True
            raise
        self._completed = True
        return result

    async def close(self) -> None:
        """Close without draining; an incomplete response is never saved."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._stream is not None:
                await self._stream.close()
        except BaseException:
            self._failed = True
            raise


class SessionPersistence:
    """Load and save whole framework sessions under a host-supplied exclusive lease.

    ``lease(id)`` must exclude every writer to that session through load, execution, cleanup,
    and save. Storage callbacks must use detached JSON snapshots and propagate failures;
    ``save`` must atomically replace the snapshot and reject an expired lease. The adapter
    supplies no storage backend, distributed lock, or retry of model work.
    """

    def __init__(
        self,
        registry: SessionCompactionRegistry,
        *,
        load: Callable[[str], Awaitable[dict[str, Any] | None]],
        save: Callable[[str, dict[str, Any]], Awaitable[None]],
        lease: Callable[[str], AbstractAsyncContextManager[None]],
    ) -> None:
        self.registry = registry
        self._load = load
        self._save = save
        self._lease = lease

    @asynccontextmanager
    async def _session(self, session_id: str, *, create: bool) -> AsyncGenerator[AgentSession]:
        if not isinstance(session_id, str) or not session_id:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("session_id must be a non-empty host-authorized string.")
        async with self._lease(session_id):
            data = await self._load(session_id)
            if data is None:
                if not create:
                    raise KeyError("Session does not exist; use create=True to allow creation.")
                session = AgentSession(session_id=session_id)
            else:
                if (
                    data.get("type") != "session"
                    or data.get("session_id") != session_id
                    or not isinstance(data.get("state"), dict)
                ):
                    raise ValueError("Stored session envelope is invalid or has a different ID.")
                session = AgentSession.from_dict(_snapshot(data))
            # Validate compaction state before any provider can observe the session.
            with self.registry.activate(session):
                pass
            yield session

    async def run[ResultT](
        self,
        session_id: str,
        call: Callable[[AgentSession], Awaitable[ResultT]],
        *,
        create: bool = False,
    ) -> ResultT:
        """Run a non-streaming request and save only after successful completion.

        Load, request, serialization, and save failures propagate. ``create`` permits a
        missing session, never replacement of corrupt or incompatible stored state.
        """
        async with self._session(session_id, create=create) as session:
            pending = call(session)
            if isinstance(pending, ResponseStream):
                await pending.close()
                raise TypeError("Use SessionPersistence.stream for streaming requests.")
            result = await pending
            if isinstance(result, ResponseStream):
                await result.close()
                raise TypeError("Use SessionPersistence.stream for streaming requests.")
            await self._save(session_id, _snapshot(session.to_dict()))
            return result

    @asynccontextmanager
    async def stream[UpdateT, ResultT](
        self,
        session_id: str,
        call: Callable[[AgentSession], ResponseStream[UpdateT, ResultT]],
        *,
        create: bool = False,
    ) -> AsyncGenerator[SessionResponseStream[UpdateT, ResultT]]:
        """Lease a session until stream cleanup and save on a clean, fully consumed exit.

        Use ``async with`` and one consumer. Only a completed, clean exit attempts a save.
        Save failures propagate after output may be delivered; their commit outcome is uncertain.
        """
        async with self._session(session_id, create=create) as session:
            # Defer construction too: an unused framework stream can own unawaited coroutines.
            response = SessionResponseStream(lambda: call(session))
            try:
                yield response
            finally:
                await response.close()
            if response.completed:
                await self._save(session_id, _snapshot(session.to_dict()))


def _snapshot(data: dict[str, Any]) -> dict[str, Any]:
    # Detach nested values so failed requests cannot mutate a store through shared references.
    return cast(dict[str, Any], json.loads(json.dumps(data, allow_nan=False)))
