"""Control abstractions and the maf-sandbox Hyperlight adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Protocol

from maf_sandbox import SandboxKey, SandboxRouter

from ._models import (
    DisposalResult,
    DisposalStatus,
    PurgeResult,
    PurgeStatus,
    SandboxRecord,
    SandboxState,
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
    key: SandboxKey
    kind: str
    instance_id: str
    state: str
    created_at: float
    last_activity_at: float
    worker_pid: int | None
    execution_contract: str | None
    egress_targets: tuple[str, ...]


class _InventoryBackend(Protocol):
    name: str

    async def list_sandboxes(self) -> Sequence[_BackendInfo]: ...


class HyperlightControl:
    """Expose an owning Hyperlight backend through safe router disposal."""

    def __init__(
        self,
        backend: _InventoryBackend,
        router: SandboxRouter,
        *,
        source_id: str,
    ) -> None:
        self._backend = backend
        self._router = router
        self._source_id = source_id

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
        if instance_id not in remaining and replacement:
            return DisposalResult(
                DisposalStatus.NOT_FOUND,
                instance_id,
                "The sandbox generation changed before disposal could be confirmed.",
            )
        if instance_id not in remaining:
            if ok:
                return DisposalResult(
                    DisposalStatus.DISPOSED,
                    instance_id,
                    "Sandbox disposed.",
                )
            return DisposalResult(
                DisposalStatus.NOT_FOUND,
                instance_id,
                "The sandbox is already gone or its generation changed.",
            )
        return DisposalResult(
            DisposalStatus.FAILED,
            instance_id,
            "Disposal was not confirmed; the backend retained the sandbox for retry.",
        )

    async def purge_thread(
        self, scope: str, thread_id: str, *, timeout: float = 10.0
    ) -> PurgeResult:
        """Purge one conversation through every backend registered with the owning router."""
        disposed = 0
        try:
            async with asyncio.timeout(timeout):
                purged = await self._router.dispose_scope(scope, thread_id)
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
