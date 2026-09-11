"""One event loop on a thread of its own, for a synchronous surface over an async backend."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
from collections.abc import Coroutine
from typing import Any

__all__ = ["SyncRunner"]


class SyncRunner:
    """Run coroutines on one loop on a daemon thread, from any thread, with or without a loop.

    One loop for the process rather than one per call, because a backend may cache a client
    per loop and never evict one for a loop that closed; a loop that lives with the process
    leaves it exactly one. A caller already holding a running loop cannot nest another, and
    ``run`` does not ask it to: the work goes to this loop's thread and the caller waits on a
    future — from any thread but the loop's own, where the wait would block the work; ``run``
    refuses that call rather than hang. Started on the first call. A fork carries the loop
    into the child but not its thread, so the child starts over on its first call, under a
    fresh guard: the inherited one may be held by a thread that did not cross.
    """

    def __init__(self, *, thread_name: str = "maf-sandbox-sync") -> None:
        self._thread_name = thread_name
        self._reset()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset)

    def _reset(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def _started(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name=self._thread_name, daemon=True
                )
                self._thread.start()
            return self._loop

    def run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run ``coroutine`` to completion and return its result; its exception is re-raised.

        Refused on this runner's own loop thread: the wait would block the one thread that
        could run the work. Code already on that loop awaits the coroutine instead.
        """
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is self._loop:
            coroutine.close()
            raise RuntimeError(
                "SyncRunner.run was called on the runner's own loop thread, where waiting "
                "would block the loop that runs the work; await the coroutine there instead"
            )
        return self.submit(coroutine).result()

    def submit[T](self, coroutine: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Run ``coroutine`` on the loop and hand back its future: work that must outlive the
        caller's loop, joinable from any loop."""
        return asyncio.run_coroutine_threadsafe(coroutine, self._started())
