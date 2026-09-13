"""Control abstractions and the maf-sandbox Hyperlight adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from maf_sandbox import SandboxKey, SandboxRouter

from ._models import DisposalResult, DisposalStatus, SandboxRecord, SandboxState


class SandboxControl(Protocol):
    """Authoritative list and exact-instance disposal operations."""

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]: ...

    async def dispose_sandbox(
        self, instance_id: str, *, timeout: float = 10.0
    ) -> DisposalResult: ...


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

    async def dispose_sandbox(self, instance_id: str, *, timeout: float = 10.0) -> DisposalResult:
        """Dispose the exact physical instance while preserving a replacement."""
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
        remaining = {current.instance_id for current in await self._backend.list_sandboxes()}
        if ok and instance_id not in remaining:
            return DisposalResult(
                DisposalStatus.DISPOSED,
                instance_id,
                "Sandbox disposed.",
            )
        return DisposalResult(
            DisposalStatus.FAILED,
            instance_id,
            "Disposal was not confirmed; the backend retained the sandbox for retry.",
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

    @classmethod
    def demo(cls, *, now: float | None = None) -> MemoryControl:
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
                source_id="research-agent",
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
            replace(
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
                source_id="policy-agent",
            ),
        ]
        return cls(records)
