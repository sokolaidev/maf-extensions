"""Host-selected authority and loop-owned SDK client leases."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, cast

from azure.core.credentials_async import AsyncTokenCredential

if TYPE_CHECKING:
    from maf_sandbox import SandboxKey


class AcasCredentialError(RuntimeError):
    """Authority resolution or client acquisition failed without an identity fallback."""


class AcasClientCloseError(RuntimeError):
    """Some SDK resources could not be drained and closed on their owning loops."""


@dataclass(frozen=True)
class AcasCredentialRequest:
    """Trusted disposal target or acquire key, independent of an originating request's lifetime."""

    scope: str
    thread_id: str
    operation: Literal["acquire", "dispose", "dispose_scope"]
    key: SandboxKey | None = None


@dataclass(frozen=True)
class AcasCredentialBinding:
    """Equivalent grants share authority and generation; each factory result is backend-owned.

    The factory must create a fresh credential on the calling loop, never return a shared
    singleton. Neither reference may contain credential material. Factories must not block.
    """

    authority: str
    generation: str
    create_credential: Callable[[], AsyncTokenCredential | Awaitable[AsyncTokenCredential]] = field(
        repr=False, compare=False
    )

    def __post_init__(self) -> None:
        for value in (self.authority, self.generation):
            if not isinstance(cast(object, value), str) or not value.strip():
                raise ValueError("authority and generation must be nonempty references")
        if not callable(self.create_credential):
            raise ValueError("create_credential must be callable")


AcasCredentialResolver = Callable[[AcasCredentialRequest], Awaitable[AcasCredentialBinding]]


def default_binding() -> AcasCredentialBinding:
    from azure.identity.aio import DefaultAzureCredential

    return AcasCredentialBinding("default-app", "0", DefaultAzureCredential)


@dataclass(eq=False)
class _Entry:
    binding: AcasCredentialBinding
    users: int = 0
    touched: float = field(default_factory=monotonic)
    client: Any = None
    credential: AsyncTokenCredential | None = None
    task: asyncio.Task[None] | None = None
    retiring: bool = False


@dataclass
class _LoopClients:
    entries: dict[tuple[str, str], _Entry] = field(default_factory=lambda: {})
    changed: Future[None] = field(default_factory=lambda: Future[None]())
    retirements: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    closing: asyncio.Task[None] | None = None


class ClientPool:
    """Bound each owning loop's clients, including construction and retirement."""

    def __init__(
        self,
        build_client: Callable[[AsyncTokenCredential], Any],
        *,
        capacity: int,
        wait_seconds: float,
        close_seconds: float,
    ) -> None:
        self._build_client = build_client
        self._capacity = capacity
        self._wait_seconds = wait_seconds
        self._close_seconds = close_seconds
        self._guard = threading.Lock()
        self._loops: dict[asyncio.AbstractEventLoop, _LoopClients] = {}
        self._closed = False

    def _notify(self, state: _LoopClients) -> None:
        previous, state.changed = state.changed, Future()
        previous.set_result(None)

    def _forget_empty(self, state: _LoopClients) -> None:
        if not state.entries and not state.retirements:
            loop = asyncio.get_running_loop()
            if self._loops.get(loop) is state:
                del self._loops[loop]

    async def _close_entry(self, entry: _Entry) -> None:
        failed = False
        for attribute in ("client", "credential"):
            resource = getattr(entry, attribute)
            if resource is None:
                continue
            try:
                async with asyncio.timeout(self._close_seconds):
                    await resource.close()
            except Exception:
                failed = True
            else:
                setattr(entry, attribute, None)
        if failed:
            raise AcasClientCloseError("ACAS client or credential closure failed")

    async def _create(self, entry: _Entry) -> None:
        try:
            async with asyncio.timeout(self._wait_seconds):
                credential = entry.binding.create_credential()
                entry.credential = (
                    await credential if inspect.isawaitable(credential) else credential
                )
                entry.client = self._build_client(entry.credential)
        except BaseException:
            # Keep partial construction owned until its bounded cleanup has finished.
            cleanup = asyncio.create_task(self._close_entry(entry))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise

    def _created(self, state: _LoopClients, key: tuple[str, str], entry: _Entry) -> None:
        task = entry.task
        assert task is not None
        failed = task.cancelled() or task.exception() is not None
        with self._guard:
            if not failed:
                # A factory may complete successfully after handling cancellation.
                entry.retiring = False
            elif not entry.users:
                if entry.client is None and entry.credential is None:
                    state.entries.pop(key, None)
                else:
                    entry.retiring = True
            self._notify(state)
            self._forget_empty(state)

    async def _retire(self, state: _LoopClients, key: tuple[str, str], entry: _Entry) -> None:
        try:
            await self._close_entry(entry)
        except AcasClientCloseError:
            # A partially closed entry cannot be reused; aclose may retry its remaining resource.
            pass
        else:
            with self._guard:
                state.entries.pop(key, None)
        finally:
            with self._guard:
                self._notify(state)

    def _retired(self, state: _LoopClients, task: asyncio.Task[None]) -> None:
        with self._guard:
            state.retirements.discard(task)
            self._notify(state)
            self._forget_empty(state)

    @asynccontextmanager
    async def lease(self, binding: AcasCredentialBinding) -> AsyncGenerator[Any, None]:
        loop = asyncio.get_running_loop()
        key = (binding.authority, binding.generation)
        entry: _Entry | None = None
        state: _LoopClients | None = None
        try:
            async with asyncio.timeout(self._wait_seconds):
                while entry is None:
                    with self._guard:
                        if self._closed:
                            raise AcasCredentialError("ACAS backend is closing or closed")
                        state = self._loops.setdefault(loop, _LoopClients())
                        changed = state.changed
                        candidate = state.entries.get(key)
                        if candidate is not None and not candidate.retiring:
                            entry = candidate
                        elif candidate is None and len(state.entries) < self._capacity:
                            entry = _Entry(binding)
                            state.entries[key] = entry
                            entry.task = loop.create_task(self._create(entry))
                            entry.task.add_done_callback(
                                lambda _task, s=state, k=key, e=entry: self._created(s, k, e)
                            )
                        if entry is not None:
                            entry.users += 1
                        else:
                            idle = [
                                (k, e)
                                for k, e in state.entries.items()
                                if not e.users and not e.retiring and e.task and e.task.done()
                            ]
                            if idle:
                                old_key, old = min(idle, key=lambda pair: pair[1].touched)
                                old.retiring = True
                                retirement = loop.create_task(self._retire(state, old_key, old))
                                state.retirements.add(retirement)
                                retirement.add_done_callback(
                                    lambda task, s=state: self._retired(s, task)
                                )
                    if entry is None:
                        await asyncio.shield(asyncio.wrap_future(changed))
                assert entry.task is not None
                await asyncio.shield(entry.task)
        except asyncio.CancelledError:
            if entry is not None and state is not None:
                self._release(state, key, entry)
            raise
        except Exception:
            if entry is not None and state is not None:
                self._release(state, key, entry)
            raise AcasCredentialError(
                "ACAS credential/client acquisition failed or timed out "
                f"(limit {self._capacity} clients per loop)"
            ) from None
        assert state is not None and entry is not None
        try:
            yield entry.client
        finally:
            self._release(state, key, entry)

    def _release(self, state: _LoopClients, key: tuple[str, str], entry: _Entry) -> None:
        with self._guard:
            entry.users -= 1
            entry.touched = monotonic()
            task = entry.task
            assert task is not None
            if not entry.users:
                if not task.done():
                    entry.retiring = True
                    task.cancel()
                elif task.cancelled() or task.exception() is not None:
                    if entry.client is None and entry.credential is None:
                        state.entries.pop(key, None)
                    else:
                        entry.retiring = True
            self._notify(state)
            self._forget_empty(state)

    async def _close_loop(self, loop: asyncio.AbstractEventLoop, state: _LoopClients) -> None:
        async with asyncio.timeout(self._close_seconds):
            while True:
                with self._guard:
                    busy = any(entry.users for entry in state.entries.values())
                    changed = state.changed
                if not busy:
                    break
                await asyncio.shield(asyncio.wrap_future(changed))
            if state.retirements:
                await asyncio.gather(*state.retirements)
            failed = False
            for key, entry in list(state.entries.items()):
                if entry.task is not None:
                    await asyncio.shield(asyncio.gather(entry.task, return_exceptions=True))
                try:
                    await self._close_entry(entry)
                except AcasClientCloseError:
                    failed = True
                    continue
                with self._guard:
                    state.entries.pop(key, None)
            if failed:
                raise AcasClientCloseError("ACAS SDK resource cleanup is incomplete")
            with self._guard:
                self._loops.pop(loop, None)

    async def _shutdown_loop(self, loop: asyncio.AbstractEventLoop, state: _LoopClients) -> None:
        if state.closing is None or state.closing.done():
            state.closing = asyncio.create_task(self._close_loop(loop, state))
            state.closing.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        await asyncio.shield(state.closing)

    async def aclose(self) -> None:
        """Quiesce every loop; refusal or timeout leaves resources available for another close."""
        current = asyncio.get_running_loop()
        with self._guard:
            self._closed = True
            for loop, state in list(self._loops.items()):
                if not state.entries and all(task.done() for task in state.retirements):
                    self._loops.pop(loop)
            states = list(self._loops.items())
            for _, state in states:
                self._notify(state)
        failed = False
        pending: list[Awaitable[None]] = []
        for loop, state in states:
            if loop is current:
                pending.append(self._shutdown_loop(loop, state))
            elif loop.is_running() and not loop.is_closed():
                closing = self._shutdown_loop(loop, state)
                try:
                    remote = asyncio.run_coroutine_threadsafe(closing, loop)
                except RuntimeError:
                    closing.close()
                    failed = True
                else:
                    pending.append(asyncio.shield(asyncio.wrap_future(remote)))
            else:
                failed = True
        try:
            async with asyncio.timeout(self._close_seconds):
                results = await asyncio.gather(*pending, return_exceptions=True)
                failed |= any(isinstance(result, BaseException) for result in results)
        except TimeoutError:
            failed = True
        if failed:
            raise AcasClientCloseError(
                "ACAS cleanup incomplete; drain operations and close on running owner loops "
                "before stopping them"
            )
