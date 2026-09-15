"""Control abstractions and monitored backend integration."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from maf_sandbox import (
    DEFAULT_BACKEND_DECLARATIONS,
    BackendDeclarations,
    DisposalFailure,
    EgressReporter,
    EgressRule,
    Isolation,
    ObservesEgress,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    ScopePurge,
)

from ._models import (
    DisposalResult,
    DisposalStatus,
    PurgeResult,
    PurgeStatus,
    SandboxRecord,
    SandboxState,
    validate_source_id,
)


class SandboxControl(Protocol):
    """Authoritative inventory and lifecycle operations."""

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]: ...

    async def get_sandbox(self, instance_id: str) -> SandboxRecord | None: ...

    async def dispose_sandbox(
        self, instance_id: str, *, timeout: float = 10.0
    ) -> DisposalResult: ...

    async def purge_thread(
        self, scope: str, thread_id: str, *, timeout: float = 10.0
    ) -> PurgeResult: ...


class _BackendInfo(Protocol):
    @property
    def key(self) -> SandboxKey: ...

    @property
    def kind(self) -> str: ...

    @property
    def instance_id(self) -> str: ...

    @property
    def state(self) -> str: ...

    @property
    def created_at(self) -> float: ...

    @property
    def last_activity_at(self) -> float: ...

    @property
    def worker_pid(self) -> int | None: ...

    @property
    def execution_contract(self) -> str | None: ...

    @property
    def egress_targets(self) -> tuple[str, ...]: ...


class _InventoryBackend(Protocol):
    @property
    def name(self) -> str: ...

    async def list_sandboxes(self) -> Sequence[_BackendInfo]: ...

    def observe_instance_disposal(
        self, key: SandboxKey, kind: str, instance_id: str
    ) -> AbstractContextManager[list[int]]: ...


@dataclass(frozen=True)
class _MonitoredInfo:
    key: SandboxKey
    kind: str
    instance_id: str
    state: str
    created_at: float
    last_activity_at: float
    worker_pid: int | None
    execution_contract: str | None
    egress_targets: tuple[str, ...]


@dataclass
class _TrackedSandbox:
    key: SandboxKey
    spec: SandboxSpec
    sandbox: Sandbox
    instance_id: str
    created_at: float
    last_activity_at: float
    published: bool = True


def _worker_pid(sandbox: Sandbox) -> int | None:
    worker = getattr(sandbox, "worker", None)
    process = getattr(worker, "process", None)
    value = getattr(process, "pid", None)
    return value if type(value) is int else None


def _worker_running(sandbox: Sandbox) -> bool | None:
    worker = getattr(sandbox, "worker", None)
    process = getattr(worker, "process", None)
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return None
    try:
        return poll() is None
    except OSError:
        return None


def _worker_exited(sandbox: Sandbox) -> bool:
    return _worker_running(sandbox) is False


def _egress_targets(spec: SandboxSpec) -> tuple[str, ...]:
    return tuple(
        sorted(
            entry.host if isinstance(entry, EgressRule) else entry for entry in spec.egress_allow
        )
    )


class MonitoredSandboxBackend:
    """Track sandboxes acquired through a backend without changing that backend's package."""

    def __init__(self, backend: SandboxBackend) -> None:
        self._backend = backend
        self._tracked: dict[tuple[SandboxKey, str], _TrackedSandbox] = {}
        self._pending: dict[tuple[SandboxKey, str], _TrackedSandbox] = {}
        self._state_lock = threading.Lock()
        self._disposal_watch: ContextVar[tuple[SandboxKey, str, str, list[int]] | None] = (
            ContextVar(f"mst_disposal_watch_{id(self)}", default=None)
        )

    @property
    def name(self) -> str:
        """Preserve the wrapped backend's routing name."""
        return self._backend.name

    @property
    def isolation(self) -> Isolation:
        """Preserve the wrapped backend's isolation declaration."""
        return self._backend.isolation

    @property
    def declarations(self) -> BackendDeclarations:
        """Preserve the wrapped backend's capability declarations."""
        return cast(
            BackendDeclarations,
            getattr(self._backend, "declarations", DEFAULT_BACKEND_DECLARATIONS),
        )

    def observe_egress(self, report: EgressReporter | None) -> EgressReporter | None:
        """Forward the router's egress reporter to the wrapped backend."""
        if isinstance(self._backend, ObservesEgress):
            return self._backend.observe_egress(report)
        return None

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        """Acquire through the backend; publication waits for router admission."""
        sandbox = await self._backend.acquire(key, spec)
        now = time.time()
        instance_id = sandbox.instance_id
        index = (key, spec.kind)
        with self._state_lock:
            previous = self._tracked.get(index)
            created_at = (
                previous.created_at
                if previous is not None and previous.instance_id == instance_id
                else now
            )
            self._pending[index] = _TrackedSandbox(
                key,
                spec,
                sandbox,
                instance_id,
                created_at,
                now,
            )
        return sandbox

    def admit_acquired(self, key: SandboxKey, spec: SandboxSpec, sandbox: Sandbox) -> None:
        """Publish only a generation returned by a successful router acquisition."""
        index = (key, spec.kind)
        with self._state_lock:
            pending = self._pending.get(index)
            if pending is not None and pending.sandbox is sandbox:
                self._tracked[index] = pending
                del self._pending[index]

    def discard_unadmitted(self, key: SandboxKey, kind: str) -> None:
        """Withhold a generation when the router refuses its acquisition."""
        with self._state_lock:
            self._pending.pop((key, kind), None)

    def _refresh_locked(self, now: float) -> None:
        for tracked in self._tracked.values():
            instance_id = tracked.sandbox.instance_id
            if instance_id != tracked.instance_id:
                tracked.instance_id = instance_id
                tracked.created_at = now
                tracked.last_activity_at = now

    async def list_sandboxes(self) -> tuple[_MonitoredInfo, ...]:
        """Return the generations observed through this wrapper."""
        now = time.time()
        with self._state_lock:
            self._refresh_locked(now)
            return tuple(
                _MonitoredInfo(
                    key=tracked.key,
                    kind=tracked.spec.kind,
                    instance_id=tracked.instance_id,
                    state=(
                        SandboxState.READY.value
                        if _worker_running(tracked.sandbox) is True
                        else SandboxState.FAILED.value
                    ),
                    created_at=tracked.created_at,
                    last_activity_at=tracked.last_activity_at,
                    worker_pid=_worker_pid(tracked.sandbox),
                    execution_contract=tracked.spec.execution_contract,
                    egress_targets=_egress_targets(tracked.spec),
                )
                for tracked in sorted(
                    self._tracked.values(),
                    key=lambda item: (
                        item.key.scope,
                        item.key.thread_id,
                        item.key.agent_id,
                        item.key.call_id,
                        item.spec.kind,
                    ),
                )
                if tracked.published
            )

    @contextmanager
    def observe_instance_disposal(
        self, key: SandboxKey, kind: str, instance_id: str
    ) -> Generator[list[int], None, None]:
        """Capture positive disposal confirmation for one control operation."""
        observed: list[int] = []
        token = self._disposal_watch.set((key, kind, instance_id, observed))
        try:
            yield observed
        finally:
            self._disposal_watch.reset(token)

    async def dispose(
        self,
        key: SandboxKey,
        *,
        kind: str | None = None,
        instance_id: str | None = None,
    ) -> DisposalFailure | None:
        """Dispose through the wrapped backend and retire confirmed tracked generations."""
        failure = await self._backend.dispose(key, kind=kind, instance_id=instance_id)
        disposed = 0
        with self._state_lock:
            self._refresh_locked(time.time())
            for index, tracked in tuple(self._tracked.items()):
                if tracked.key != key or (kind is not None and tracked.spec.kind != kind):
                    continue
                if instance_id is not None and tracked.instance_id != instance_id:
                    continue
                if failure is None and _worker_exited(tracked.sandbox):
                    del self._tracked[index]
                    disposed += 1
        watch = self._disposal_watch.get()
        if watch is not None and (key, kind, instance_id) == watch[:3]:
            watch[3].append(disposed)
        return failure

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        """Purge through the wrapped backend without publishing ambiguous generations."""
        result = await self._backend.dispose_scope(scope, thread_id)
        with self._state_lock:
            self._refresh_locked(time.time())
            for index, tracked in tuple(self._tracked.items()):
                if tracked.key.scope == scope and tracked.key.thread_id == thread_id:
                    if _worker_exited(tracked.sandbox):
                        del self._tracked[index]
                    elif result.undisposed is not None and result.disposed:
                        tracked.published = False
        return result


class MonitoredSandboxRouter(SandboxRouter):
    """Publish monitored generations only after the owning router admits them."""

    async def acquire(self, key: SandboxKey, spec: SandboxSpec, **kwargs: Any) -> Sandbox:
        monitored = tuple(
            backend for backend in self._backends if isinstance(backend, MonitoredSandboxBackend)
        )
        try:
            sandbox = await super().acquire(key, spec, **kwargs)
        except BaseException:
            for backend in monitored:
                backend.discard_unadmitted(key, spec.kind)
            raise
        for backend in monitored:
            backend.admit_acquired(key, spec, sandbox)
        return sandbox


class HyperlightControl:
    """Expose a monitored Hyperlight backend through safe router disposal.

    Host quiescence callbacks must fence new work and drain active calls across replicas.
    Without them, their respective disposal operations are disabled.
    """

    def __init__(
        self,
        backend: _InventoryBackend,
        router: SandboxRouter,
        *,
        source_id: str,
        quiesce_instance: Callable[[SandboxKey], AbstractAsyncContextManager[None]] | None = None,
        quiesced_purge: Callable[[str, str], Awaitable[ScopePurge]] | None = None,
    ) -> None:
        self._backend = backend
        self._router = router
        self._source_id = validate_source_id(source_id)
        self._quiesce_instance = quiesce_instance
        self._quiesced_purge = quiesced_purge

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
        """Snapshot the backend registry without transferring sandbox authority."""
        records: list[SandboxRecord] = []
        for item in await self._backend.list_sandboxes():
            records.append(
                SandboxRecord(
                    source_id=self._source_id,
                    backend=self._backend.name,
                    scope=item.key.scope,
                    thread_id=item.key.thread_id,
                    agent_id=item.key.agent_id,
                    call_id=item.key.call_id,
                    kind=item.kind,
                    instance_id=item.instance_id,
                    state=SandboxState(item.state),
                    created_at=item.created_at,
                    last_activity_at=item.last_activity_at,
                    process_id=item.worker_pid,
                    execution_contract=item.execution_contract,
                    egress_targets=item.egress_targets,
                )
            )
        return tuple(records)

    async def get_sandbox(self, instance_id: str) -> SandboxRecord | None:
        """Return one physical instance when this host still owns it."""
        return next(
            (item for item in await self.list_sandboxes() if item.instance_id == instance_id),
            None,
        )

    async def dispose_sandbox(self, instance_id: str, *, timeout: float = 10.0) -> DisposalResult:
        """Dispose the exact physical instance while preserving a replacement."""
        try:
            async with asyncio.timeout(timeout):
                before = {item.instance_id: item for item in await self._backend.list_sandboxes()}
                item = before.get(instance_id)
                if item is None:
                    return DisposalResult(
                        DisposalStatus.NOT_FOUND,
                        instance_id,
                        "The sandbox is already gone or its generation changed.",
                    )
                if self._quiesce_instance is None:
                    return DisposalResult(
                        DisposalStatus.FAILED,
                        instance_id,
                        "Exact disposal is disabled because the host did not configure "
                        "a quiescence boundary.",
                    )
                async with self._quiesce_instance(item.key):
                    current = await self._backend.list_sandboxes()
                    if not any(
                        entry.instance_id == instance_id
                        and entry.key == item.key
                        and entry.kind == item.kind
                        for entry in current
                    ):
                        return DisposalResult(
                            DisposalStatus.NOT_FOUND,
                            instance_id,
                            "The sandbox generation changed before disposal could be confirmed.",
                        )
                    with self._backend.observe_instance_disposal(
                        item.key, item.kind, instance_id
                    ) as observed:
                        ok = await self._router.dispose_kind(
                            item.key,
                            item.kind,
                            instance_id=instance_id,
                            timeout=timeout,
                        )
                    after = await self._backend.list_sandboxes()
        except TimeoutError:
            return DisposalResult(
                DisposalStatus.FAILED,
                instance_id,
                "Disposal timed out and was not confirmed.",
            )
        remaining = {current.instance_id for current in after}
        replacement = any(
            current.key == item.key
            and current.kind == item.kind
            and current.instance_id != instance_id
            for current in after
        )
        disposed = sum(observed)
        if not ok:
            return DisposalResult(
                DisposalStatus.FAILED,
                instance_id,
                "A registered backend reported a disposal failure.",
            )
        if instance_id not in remaining and ok and disposed > 0:
            return DisposalResult(
                DisposalStatus.DISPOSED,
                instance_id,
                "Sandbox disposed.",
            )
        if instance_id not in remaining and replacement:
            return DisposalResult(
                DisposalStatus.NOT_FOUND,
                instance_id,
                "The sandbox generation changed before disposal could be confirmed.",
            )
        if instance_id not in remaining:
            status = DisposalStatus.NOT_FOUND if observed else DisposalStatus.FAILED
            message = (
                "The sandbox is already gone or its generation changed."
                if observed
                else "Disposal completed without a matching backend receipt."
            )
            return DisposalResult(status, instance_id, message)
        return DisposalResult(
            DisposalStatus.FAILED,
            instance_id,
            "Disposal was not confirmed; the backend retained the sandbox for retry.",
        )

    async def purge_thread(
        self, scope: str, thread_id: str, *, timeout: float = 10.0
    ) -> PurgeResult:
        """Purge one conversation through the host's quiescence boundary."""
        if self._quiesced_purge is None:
            return PurgeResult(
                PurgeStatus.PARTIAL,
                scope,
                thread_id,
                0,
                "Conversation purge is disabled because the host did not configure "
                "a quiescence boundary.",
            )
        disposed = 0
        try:
            async with asyncio.timeout(timeout):
                purged = await self._quiesced_purge(scope, thread_id)
                disposed = purged.disposed
                remaining = tuple(
                    item
                    for item in await self._backend.list_sandboxes()
                    if item.key.scope == scope and item.key.thread_id == thread_id
                )
        except TimeoutError:
            return PurgeResult(
                PurgeStatus.PARTIAL,
                scope,
                thread_id,
                disposed,
                "Conversation purge timed out and was not confirmed.",
            )
        complete = purged.undisposed is None and not remaining
        if complete:
            return PurgeResult(
                PurgeStatus.PURGED,
                scope,
                thread_id,
                purged.disposed,
                "Conversation purged.",
            )
        detail = (
            "matching sandboxes remain" if purged.undisposed is None else purged.undisposed.detail
        )
        return PurgeResult(
            PurgeStatus.PARTIAL,
            scope,
            thread_id,
            purged.disposed,
            f"Conversation purge was not confirmed: {detail}",
        )


class MemoryControl:
    """Deterministic control source for demonstrations and UI tests."""

    def __init__(self, records: Sequence[SandboxRecord] = ()) -> None:
        self._records = {record.instance_id: record for record in records}
        self._lock = asyncio.Lock()

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
        """Return records ordered by logical key."""
        async with self._lock:
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda item: (item.source_id, item.logical_name, item.kind),
                )
            )

    async def get_sandbox(self, instance_id: str) -> SandboxRecord | None:
        """Return one physical instance when present."""
        async with self._lock:
            return self._records.get(instance_id)

    async def dispose_sandbox(self, instance_id: str, *, timeout: float = 10.0) -> DisposalResult:
        """Remove exactly one record."""
        del timeout
        async with self._lock:
            if self._records.pop(instance_id, None) is None:
                return DisposalResult(
                    DisposalStatus.NOT_FOUND,
                    instance_id,
                    "The sandbox is already gone or its generation changed.",
                )
        return DisposalResult(DisposalStatus.DISPOSED, instance_id, "Sandbox disposed.")

    async def purge_thread(
        self, scope: str, thread_id: str, *, timeout: float = 10.0
    ) -> PurgeResult:
        """Remove every demonstration record belonging to one conversation."""
        del timeout
        async with self._lock:
            selected = [
                instance_id
                for instance_id, record in self._records.items()
                if record.scope == scope and record.thread_id == thread_id
            ]
            for instance_id in selected:
                del self._records[instance_id]
        return PurgeResult(
            PurgeStatus.PURGED,
            scope,
            thread_id,
            len(selected),
            "Conversation purged.",
        )

    @classmethod
    def demo(cls, *, now: float | None = None, source_id: str = "research-agent") -> MemoryControl:
        """Build a representative local Hyperlight inventory."""
        stamp = time.time() if now is None else now

        def record(
            *,
            thread_id: str,
            agent_id: str,
            kind: str,
            instance_id: str,
            state: SandboxState,
            created_at: float,
            last_activity_at: float,
            process_id: int | None,
            egress_targets: tuple[str, ...] = (),
        ) -> SandboxRecord:
            return SandboxRecord(
                source_id=source_id,
                backend="hyperlight",
                scope="tenant-labs",
                thread_id=thread_id,
                agent_id=agent_id,
                call_id="",
                kind=kind,
                instance_id=instance_id,
                state=state,
                created_at=created_at,
                last_activity_at=last_activity_at,
                process_id=process_id,
                execution_contract="python-3.14-wasm",
                egress_targets=egress_targets,
            )

        records = [
            record(
                thread_id="incident-184",
                agent_id="analyst",
                kind="codeact",
                instance_id="9f15a25d8c0d4ea0ab1af045d9481001",
                state=SandboxState.RUNNING,
                created_at=stamp - 184,
                last_activity_at=stamp - 2,
                process_id=28440,
                egress_targets=("https://api.github.com/",),
            ),
            record(
                thread_id="forecast-042",
                agent_id="planner",
                kind="codeact",
                instance_id="f2ecba87b2ce44659a66fd28fd0a1002",
                state=SandboxState.READY,
                created_at=stamp - 623,
                last_activity_at=stamp - 91,
                process_id=30112,
            ),
            record(
                thread_id="review-711",
                agent_id="reviewer",
                kind="python",
                instance_id="7720e57b3ad7441ca21f5a7392ce1003",
                state=SandboxState.FAILED,
                created_at=stamp - 71,
                last_activity_at=stamp - 11,
                process_id=None,
            ),
        ]
        return cls(records)


if TYPE_CHECKING:
    _binding: tuple[SandboxBackend, type[Sandbox]] = (
        MonitoredSandboxBackend(cast(SandboxBackend, object())),
        type(cast(Sandbox, object())),
    )
