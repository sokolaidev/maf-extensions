"""Control records, local discovery and exact-instance disposal."""

from __future__ import annotations

import asyncio
import getpass
import json
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import cast

import pytest
from maf_sandbox import (
    BackendDeclarations,
    Capability,
    DisposalFailure,
    Egress,
    EgressReporter,
    EgressRule,
    Isolation,
    Sandbox,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    ScopePurge,
)
from maf_sandbox.conformance import (
    ConformanceSubject,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_reclaim_conformance,
)

import maf_sandbox_tui._server as server_module
import maf_sandbox_tui.cli as cli_module
from maf_sandbox_tui import (
    CompositeControl,
    ControlEndpointError,
    DisposalResult,
    DisposalStatus,
    EndpointManifest,
    HttpControl,
    HyperlightControl,
    MemoryControl,
    MonitoredSandboxBackend,
    PurgeResult,
    PurgeStatus,
    SandboxControlServer,
    SandboxRecord,
    SandboxState,
    discover_controls,
)
from maf_sandbox_tui._client import PartialInventoryError, read_manifests


@dataclass(frozen=True)
class _Info:
    key: SandboxKey
    kind: str
    instance_id: str
    state: str = "ready"
    created_at: float = 900
    last_activity_at: float = 999
    worker_pid: int | None = 42
    execution_contract: str | None = "python-3.14-wasm"
    egress_targets: tuple[str, ...] = ()


class _Inventory:
    name = "hyperlight"

    def __init__(self, records: list[_Info]) -> None:
        self.records = records
        self._disposal_watch: tuple[SandboxKey, str, str, list[int]] | None = None

    async def list_sandboxes(self) -> tuple[_Info, ...]:
        return tuple(self.records)

    @contextmanager
    def observe_instance_disposal(
        self, key: SandboxKey, kind: str, instance_id: str
    ) -> Generator[list[int], None, None]:
        observed: list[int] = []
        self._disposal_watch = (key, kind, instance_id, observed)
        try:
            yield observed
        finally:
            self._disposal_watch = None


class _Router:
    def __init__(
        self,
        inventory: _Inventory,
        replacement: _Info | None,
        *,
        succeeds: bool = True,
        disposed: int | None = None,
    ) -> None:
        self.inventory = inventory
        self.replacement = replacement
        self.succeeds = succeeds
        self.disposed = (
            (1 if succeeds and replacement is None else 0) if disposed is None else disposed
        )
        self.calls: list[tuple[SandboxKey, str, str | None, float]] = []
        self.scope_calls: list[tuple[str, str]] = []

    async def dispose_kind(
        self,
        key: SandboxKey,
        kind: str,
        *,
        instance_id: str | None = None,
        timeout: float,
    ) -> bool:
        self.calls.append((key, kind, instance_id, timeout))
        watch = self.inventory._disposal_watch
        if watch is not None and (key, kind, instance_id) == watch[:3]:
            watch[3].append(self.disposed)
        self.inventory.records = [] if self.replacement is None else [self.replacement]
        return self.succeeds

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        self.scope_calls.append((scope, thread_id))
        before = len(self.inventory.records)
        self.inventory.records = [
            item
            for item in self.inventory.records
            if (item.key.scope, item.key.thread_id) != (scope, thread_id)
        ]
        return ScopePurge(before - len(self.inventory.records))


class _ObservedSandbox:
    def __init__(self, instance_id: str, *, pid: object = 42) -> None:
        self.instance_id = instance_id
        self.alive: object = True
        self._gate = threading.Lock()
        self.worker = SimpleNamespace(process=SimpleNamespace(pid=pid))

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        del directory, working_directory, timeout
        raise NotImplementedError

    async def reset(self, *, timeout: float) -> None:
        del timeout
        self.instance_id = f"{self.instance_id}-reset"


class _ObservedBackend:
    name = "hyperlight"
    isolation = Isolation.MICROVM
    declarations = BackendDeclarations(
        capabilities=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.SNAPSHOT}),
        egress_modes=frozenset({Egress.CLOSED, Egress.ALLOWLIST}),
    )

    def __init__(self, *, pid: object = 42) -> None:
        self.pid = pid
        self.sandboxes: dict[tuple[SandboxKey, str], _ObservedSandbox] = {}
        self.disposals: list[tuple[SandboxKey, str | None, str | None]] = []
        self.failure: DisposalFailure | None = None
        self.noop = False

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        sandbox = self.sandboxes.setdefault(
            (key, spec.kind),
            _ObservedSandbox(f"generation-{len(self.sandboxes) + 1}", pid=self.pid),
        )
        return cast(Sandbox, sandbox)

    async def dispose(
        self,
        key: SandboxKey,
        *,
        kind: str | None = None,
        instance_id: str | None = None,
    ) -> DisposalFailure | None:
        self.disposals.append((key, kind, instance_id))
        if self.failure is not None or self.noop:
            return self.failure
        for index, sandbox in tuple(self.sandboxes.items()):
            if index[0] != key or (kind is not None and index[1] != kind):
                continue
            if instance_id is not None and sandbox.instance_id != instance_id:
                continue
            sandbox.alive = False
            del self.sandboxes[index]
        return None

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        if self.failure is not None:
            return ScopePurge(0, self.failure)
        disposed = 0
        for index, sandbox in tuple(self.sandboxes.items()):
            if index[0].scope == scope and index[0].thread_id == thread_id:
                sandbox.alive = False
                del self.sandboxes[index]
                disposed += 1
        return ScopePurge(disposed)


class _EgressBackend(_ObservedBackend):
    declarations = BackendDeclarations(observes_egress=True)

    def __init__(self) -> None:
        super().__init__()
        self.reporter: EgressReporter | None = None

    def observe_egress(self, report: EgressReporter | None) -> EgressReporter | None:
        previous, self.reporter = self.reporter, report
        return previous


def test_record_json_round_trip_preserves_the_physical_identity():
    records = asyncio.run(MemoryControl.demo(now=1_000).list_sandboxes())
    record = next(item for item in records if item.thread_id == "forecast-042")
    assert SandboxRecord.from_json(record.to_json()) == record
    assert record.logical_name == "tenant-labs/forecast-042/planner"


def test_record_json_refuses_malformed_process_identity():
    record = asyncio.run(MemoryControl.demo(now=1_000).list_sandboxes())[0]
    value = record.to_json()
    value["process_id"] = True
    with pytest.raises(ValueError, match="process_id"):
        SandboxRecord.from_json(value)


@pytest.mark.parametrize("field", ["created_at", "last_activity_at"])
@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
def test_record_json_refuses_non_finite_timestamps(field: str, timestamp: float):
    record = asyncio.run(MemoryControl.demo(now=1_000).list_sandboxes())[0]
    value = record.to_json()
    value[field] = timestamp
    with pytest.raises(ValueError, match=rf"{field} must be finite"):
        SandboxRecord.from_json(value)


def test_memory_control_disposes_only_the_named_generation():
    async def check() -> None:
        record = (await MemoryControl.demo(now=1_000).list_sandboxes())[0]
        replacement = replace(record, instance_id="replacement", state=SandboxState.READY)
        control = MemoryControl([record, replacement])
        result = await control.dispose_sandbox(record.instance_id)
        assert result.status is DisposalStatus.DISPOSED
        assert await control.list_sandboxes() == (replacement,)
        stale = await control.dispose_sandbox(record.instance_id)
        assert stale.status is DisposalStatus.NOT_FOUND

    asyncio.run(check())


def test_composite_control_cannot_confirm_a_purge_without_hosts():
    async def check() -> None:
        result = await CompositeControl(()).purge_thread("tenant-labs", "thread-1")
        assert result.status is PurgeStatus.PARTIAL
        assert result.disposed == 0

    asyncio.run(check())


def test_composite_control_preserves_partial_inventory_errors():
    class FailingControl(MemoryControl):
        async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
            raise ControlEndpointError("host stopped")

    async def check() -> None:
        record = (await MemoryControl.demo(now=1_000).list_sandboxes())[0]
        control = CompositeControl((MemoryControl([record]), FailingControl()))

        with pytest.raises(PartialInventoryError, match="incomplete") as raised:
            await control.list_sandboxes()

        assert raised.value.records == (record,)
        assert raised.value.errors == ("host stopped",)

    asyncio.run(check())


def test_initial_probe_failure_reaches_the_console_control():
    manifest = EndpointManifest("stopped-host", "http://127.0.0.1:1", 1)
    probe = cli_module._HostProbe(manifest, HttpControl(manifest), "connection refused")
    control = cli_module._control((probe,))

    async def check() -> None:
        with pytest.raises(PartialInventoryError, match="incomplete") as raised:
            await control.list_sandboxes()
        assert raised.value.records == ()
        assert raised.value.errors == ("stopped-host: connection refused",)

    asyncio.run(check())


def test_composite_lookup_does_not_turn_an_outage_into_not_found():
    class FailingControl(MemoryControl):
        async def get_sandbox(self, instance_id: str) -> SandboxRecord | None:
            del instance_id
            raise ControlEndpointError("host stopped")

    async def check() -> None:
        with pytest.raises(ControlEndpointError, match="could not be confirmed"):
            await CompositeControl((FailingControl(),)).get_sandbox("generation-a")

    asyncio.run(check())


def test_composite_disposal_locates_hosts_concurrently():
    async def check() -> None:
        record = (await MemoryControl.demo(now=1_000).list_sandboxes())[0]
        both_started = asyncio.Event()
        started = 0

        class CoordinatedControl(MemoryControl):
            async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
                nonlocal started
                started += 1
                if started == 2:
                    both_started.set()
                await both_started.wait()
                return await super().list_sandboxes()

        owner = MemoryControl((record,))
        control = CompositeControl((CoordinatedControl(), CoordinatedControl(), owner))

        result = await control.dispose_sandbox(record.instance_id, timeout=0.2)

        assert result.status is DisposalStatus.DISPOSED
        assert started == 2
        assert await owner.list_sandboxes() == ()

    asyncio.run(check())


def test_composite_disposal_is_bounded_while_locating_hosts():
    class HangingControl(MemoryControl):
        async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def check() -> None:
        result = await CompositeControl((HangingControl(),)).dispose_sandbox(
            "generation-a", timeout=0.01
        )
        assert result.status is DisposalStatus.FAILED
        assert result.message == "Disposal timed out while locating the owning host."

    asyncio.run(check())


def test_composite_disposal_refuses_an_unconfirmed_single_owner():
    class FailingControl(MemoryControl):
        async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
            raise ControlEndpointError("host stopped")

    async def check() -> None:
        record = (await MemoryControl.demo(now=1_000).list_sandboxes())[0]
        owner = MemoryControl((record,))

        result = await CompositeControl((owner, FailingControl())).dispose_sandbox(
            record.instance_id
        )

        assert result.status is DisposalStatus.FAILED
        assert result.message == (
            "Sandbox ownership could not be confirmed on 1 unavailable host(s)."
        )
        assert await owner.list_sandboxes() == (record,)

    asyncio.run(check())


def test_composite_purge_cancels_hosts_at_the_shared_deadline():
    cancelled = False

    class HangingControl(MemoryControl):
        async def purge_thread(
            self, scope: str, thread_id: str, *, timeout: float = 10.0
        ) -> PurgeResult:
            del scope, thread_id, timeout
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True
            raise AssertionError("unreachable")

    async def check() -> None:
        record = (await MemoryControl.demo(now=1_000).list_sandboxes())[0]
        result = await CompositeControl((MemoryControl((record,)), HangingControl())).purge_thread(
            record.scope, record.thread_id, timeout=0.01
        )

        assert result.status is PurgeStatus.PARTIAL
        assert result.disposed == 1
        assert result.message == "Conversation purge timed out before 1 host(s) responded."
        assert cancelled

    asyncio.run(check())


def test_monitored_backend_tracks_only_acquisitions_through_the_wrapper():
    async def check() -> None:
        inner = _ObservedBackend()
        monitored = MonitoredSandboxBackend(inner)
        key = SandboxKey("tenant-labs", "thread-1", "agent-1", "call-1")
        spec = SandboxSpec(
            kind="codeact",
            work_dir=None,
            egress=Egress.ALLOWLIST,
            egress_allow=("z.example", EgressRule("a.example")),
            execution_contract="python-wasm",
        )

        sandbox = await monitored.acquire(key, spec)
        records = await monitored.list_sandboxes()

        assert monitored.name == inner.name
        assert monitored.isolation is inner.isolation
        assert monitored.declarations is inner.declarations
        assert len(records) == 1
        assert records[0].key == key
        assert records[0].instance_id == sandbox.instance_id
        assert records[0].state == "ready"
        assert records[0].worker_pid == 42
        assert records[0].execution_contract == "python-wasm"
        assert records[0].egress_targets == ("a.example", "z.example")

        created_at = records[0].created_at
        assert await monitored.acquire(key, spec) is sandbox
        assert (await monitored.list_sandboxes())[0].created_at == created_at

        concrete = inner.sandboxes[(key, spec.kind)]
        concrete._gate.acquire()
        try:
            assert (await monitored.list_sandboxes())[0].state == "running"
        finally:
            concrete._gate.release()
        concrete.instance_id = "replacement"
        replaced = (await monitored.list_sandboxes())[0]
        assert replaced.instance_id == "replacement"
        assert replaced.created_at >= created_at

    asyncio.run(check())


def test_monitored_backend_forwards_egress_reporter_install_and_removal():
    inner = _EgressBackend()
    monitored = MonitoredSandboxBackend(inner)

    def first(_event: object) -> None:
        return None

    def second(_event: object) -> None:
        return None

    assert monitored.observe_egress(first) is None
    assert inner.reporter is first
    assert monitored.observe_egress(second) is first
    assert inner.reporter is second
    assert monitored.observe_egress(None) is second
    assert inner.reporter is None

    SandboxRouter([monitored], observer=SandboxObserver())
    assert inner.reporter is not None
    SandboxRouter([monitored])
    assert inner.reporter is None


def test_monitored_backend_accepts_router_reporting_for_a_nonobserving_backend():
    monitored = MonitoredSandboxBackend(_ObservedBackend())

    SandboxRouter([monitored], observer=SandboxObserver())

    assert monitored.observe_egress(None) is None


def test_monitored_backend_tolerates_unknown_runtime_metadata():
    async def check() -> None:
        inner = _ObservedBackend(pid=True)
        monitored = MonitoredSandboxBackend(inner)
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        spec = SandboxSpec(kind="codeact", work_dir=None)
        await monitored.acquire(key, spec)
        inner.sandboxes[(key, spec.kind)].alive = "unknown"
        record = (await monitored.list_sandboxes())[0]
        assert record.state == "ready"
        assert record.worker_pid is None

    asyncio.run(check())


def test_monitored_backend_answers_withheld_core_conformance():
    inner = _ObservedBackend()
    inner.declarations = BackendDeclarations(capabilities=frozenset())
    monitored = MonitoredSandboxBackend(inner)
    subject = cast(
        ConformanceSubject,
        SimpleNamespace(capabilities=monitored.declarations.capabilities),
    )

    async def check() -> None:
        with pytest.raises(ValueError, match="FILES_IN"):
            await assert_files_in_conformance(subject)
        with pytest.raises(ValueError, match="EXEC"):
            await assert_exec_conformance(subject)
        with pytest.raises(ValueError, match="FILES_DELETE"):
            await assert_files_delete_conformance(subject)
        with pytest.raises(ValueError, match="RECLAIM"):
            await assert_reclaim_conformance(subject)

    asyncio.run(check())


def test_monitored_backend_filters_direct_exact_disposal():
    async def check() -> None:
        inner = _ObservedBackend()
        monitored = MonitoredSandboxBackend(inner)
        target = SandboxKey("tenant-labs", "thread-1", "agent-1")
        survivor = SandboxKey("tenant-labs", "thread-2", "agent-1")
        spec = SandboxSpec(kind="codeact", work_dir=None)
        sandbox = await monitored.acquire(target, spec)
        await monitored.acquire(survivor, spec)

        assert (
            await monitored.dispose(target, kind=spec.kind, instance_id="not-the-owned-generation")
            is None
        )
        assert len(await monitored.list_sandboxes()) == 2

        assert (
            await monitored.dispose(target, kind=spec.kind, instance_id=sandbox.instance_id) is None
        )
        assert [item.key for item in await monitored.list_sandboxes()] == [survivor]

    asyncio.run(check())


def test_monitored_backend_supplies_authoritative_exact_disposal_receipts():
    async def check() -> None:
        inner = _ObservedBackend()
        monitored = MonitoredSandboxBackend(inner)
        router = SandboxRouter([monitored])
        control = HyperlightControl(monitored, router, source_id="agent-app")
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        spec = SandboxSpec(kind="codeact", work_dir=None)
        sandbox = await router.acquire(key, spec)

        found = await control.get_sandbox(sandbox.instance_id)
        assert found is not None and found.instance_id == sandbox.instance_id
        assert await control.get_sandbox("missing") is None
        missing = await control.dispose_sandbox("missing")
        assert missing.status is DisposalStatus.NOT_FOUND

        result = await control.dispose_sandbox(sandbox.instance_id, timeout=3.0)

        assert result.status is DisposalStatus.DISPOSED
        assert inner.disposals == [(key, spec.kind, sandbox.instance_id)]
        assert await monitored.list_sandboxes() == ()

    asyncio.run(check())


def test_monitored_backend_retains_an_unconfirmed_or_failed_disposal():
    async def check(*, failure: DisposalFailure | None) -> None:
        inner = _ObservedBackend()
        monitored = MonitoredSandboxBackend(inner)
        router = SandboxRouter([monitored])
        control = HyperlightControl(monitored, router, source_id="agent-app")
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        sandbox = await router.acquire(key, SandboxSpec(kind="codeact", work_dir=None))
        inner.noop = failure is None
        inner.failure = failure

        result = await control.dispose_sandbox(sandbox.instance_id)

        assert result.status is DisposalStatus.FAILED
        assert (await monitored.list_sandboxes())[0].instance_id == sandbox.instance_id

    asyncio.run(check(failure=None))
    asyncio.run(check(failure=DisposalFailure("unknown", "worker close failed")))


def test_monitored_backend_purges_only_the_named_conversation():
    async def check() -> None:
        inner = _ObservedBackend()
        monitored = MonitoredSandboxBackend(inner)
        router = SandboxRouter([monitored])
        control = HyperlightControl(
            monitored,
            router,
            source_id="agent-app",
            quiesced_purge=router.dispose_scope,
        )
        target = SandboxKey("tenant-labs", "thread-1", "agent-1")
        survivor = SandboxKey("tenant-labs", "thread-2", "agent-1")
        spec = SandboxSpec(kind="codeact", work_dir=None)
        await router.acquire(target, spec)
        await router.acquire(survivor, spec)

        result = await control.purge_thread(target.scope, target.thread_id)

        assert result.status is PurgeStatus.PURGED
        assert result.disposed == 1
        records = await monitored.list_sandboxes()
        assert len(records) == 1 and records[0].key == survivor

        inner.failure = DisposalFailure("unknown", "provider unavailable")
        partial = await control.purge_thread(survivor.scope, survivor.thread_id)
        assert partial.status is PurgeStatus.PARTIAL
        assert len(await monitored.list_sandboxes()) == 1

    asyncio.run(check())


def test_hyperlight_control_routes_exact_generation():
    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        inventory = _Inventory([target])
        router = _Router(inventory, None)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        listed = await control.list_sandboxes()
        assert listed[0].source_id == "agent-app"
        assert listed[0].logical_name == "tenant-labs/thread-1/agent-1"
        result = await control.dispose_sandbox(target.instance_id, timeout=3.0)

        assert result.status is DisposalStatus.DISPOSED
        assert router.calls == [(key, "codeact", "generation-a", 3.0)]
        assert await inventory.list_sandboxes() == ()

    asyncio.run(check())


def test_hyperlight_control_reports_a_concurrent_replacement_as_stale():
    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        replacement = replace(target, instance_id="generation-b")
        inventory = _Inventory([target])
        router = _Router(inventory, replacement)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        result = await control.dispose_sandbox(target.instance_id, timeout=3.0)

        assert result.status is DisposalStatus.NOT_FOUND
        assert (
            result.message == "The sandbox generation changed before disposal could be confirmed."
        )
        assert [item.instance_id for item in await inventory.list_sandboxes()] == ["generation-b"]

    asyncio.run(check())


def test_hyperlight_control_reports_a_router_failure_even_when_the_instance_disappears():
    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        inventory = _Inventory([target])
        router = _Router(inventory, None, succeeds=False)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        result = await control.dispose_sandbox(target.instance_id, timeout=3.0)

        assert result.status is DisposalStatus.FAILED
        assert result.message == "A registered backend reported a disposal failure."

    asyncio.run(check())


def test_hyperlight_control_requires_a_positive_backend_disposal_receipt():
    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        inventory = _Inventory([target])
        router = _Router(inventory, None, disposed=0)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        result = await control.dispose_sandbox(target.instance_id, timeout=3.0)

        assert result.status is DisposalStatus.NOT_FOUND
        assert result.message == "The sandbox is already gone or its generation changed."

    asyncio.run(check())


def test_hyperlight_control_preserves_a_router_failure_after_a_positive_receipt():
    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        inventory = _Inventory([target])
        router = _Router(inventory, None, succeeds=False, disposed=1)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        result = await control.dispose_sandbox(target.instance_id, timeout=3.0)

        assert result.status is DisposalStatus.FAILED
        assert result.message == "A registered backend reported a disposal failure."

    asyncio.run(check())


def test_hyperlight_control_refuses_an_unfenced_conversation_purge():
    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        inventory = _Inventory([target])
        router = _Router(inventory, None)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        result = await control.purge_thread(key.scope, key.thread_id)

        assert result.status is PurgeStatus.PARTIAL
        assert result.disposed == 0
        assert "did not configure a quiescence boundary" in result.message
        assert router.scope_calls == []
        assert inventory.records == [target]

    asyncio.run(check())


def test_hyperlight_control_purges_the_conversation_through_the_router():
    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        survivor = _Info(
            SandboxKey("tenant-labs", "thread-2", "agent-1"),
            "codeact",
            "generation-b",
        )
        inventory = _Inventory([target, survivor])
        router = _Router(inventory, survivor)
        control = HyperlightControl(
            inventory,
            cast(SandboxRouter, router),
            source_id="agent-app",
            quiesced_purge=router.dispose_scope,
        )

        result = await control.purge_thread("tenant-labs", "thread-1")

        assert result.status is PurgeStatus.PURGED
        assert result.disposed == 1
        assert router.scope_calls == [("tenant-labs", "thread-1")]
        assert [item.instance_id for item in inventory.records] == ["generation-b"]

    asyncio.run(check())


def test_hyperlight_control_bounds_disposal_inventory():
    class SlowInventory(_Inventory):
        async def list_sandboxes(self) -> tuple[_Info, ...]:
            await asyncio.sleep(1)
            return await super().list_sandboxes()

    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        inventory = SlowInventory([target])
        router = _Router(inventory, target)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        result = await control.dispose_sandbox(target.instance_id, timeout=0.01)

        assert result.status is DisposalStatus.FAILED
        assert result.message == "Disposal timed out and was not confirmed."
        assert router.calls == []

    asyncio.run(check())


def test_hyperlight_control_bounds_disposal_confirmation():
    class SlowSecondInventory(_Inventory):
        def __init__(self, records: list[_Info]) -> None:
            super().__init__(records)
            self.calls = 0

        async def list_sandboxes(self) -> tuple[_Info, ...]:
            self.calls += 1
            if self.calls == 2:
                await asyncio.sleep(1)
            return await super().list_sandboxes()

    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        replacement = replace(target, instance_id="generation-b")
        inventory = SlowSecondInventory([target])
        router = _Router(inventory, replacement)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        result = await control.dispose_sandbox(target.instance_id, timeout=0.01)

        assert result.status is DisposalStatus.FAILED
        assert result.message == "Disposal timed out and was not confirmed."
        assert router.calls == [(key, "codeact", "generation-a", 0.01)]

    asyncio.run(check())


def test_hyperlight_control_bounds_post_purge_inventory():
    class SlowAfterPurgeInventory(_Inventory):
        async def list_sandboxes(self) -> tuple[_Info, ...]:
            await asyncio.sleep(1)
            return await super().list_sandboxes()

    class ImmediateRouter(_Router):
        async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
            self.scope_calls.append((scope, thread_id))
            return ScopePurge(1)

    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        inventory = SlowAfterPurgeInventory([target])
        router = ImmediateRouter(inventory, target)
        control = HyperlightControl(
            inventory,
            cast(SandboxRouter, router),
            source_id="agent-app",
            quiesced_purge=router.dispose_scope,
        )

        result = await control.purge_thread("tenant-labs", "thread-1", timeout=0.01)

        assert result.status is PurgeStatus.PARTIAL
        assert result.disposed == 1
        assert result.message == "Conversation purge timed out and was not confirmed."

    asyncio.run(check())


def test_loopback_endpoint_lists_and_disposes_end_to_end(tmp_path):
    async def check() -> None:
        control = MemoryControl.demo(now=1_000)
        async with SandboxControlServer(
            control,
            source_id="test-host",
            manifest_directory=tmp_path,
        ) as server:
            assert read_manifests(tmp_path) == (server.manifest,)
            manifest_data = json.loads(next(tmp_path.glob("*.json")).read_text("utf-8"))
            assert "token" not in manifest_data
            client = HttpControl(server.manifest)
            await client.health()
            await HttpControl(replace(server.manifest, endpoint=f"{server.endpoint}/")).health()
            records = await client.list_sandboxes()
            assert len(records) == 3
            result = await client.dispose_sandbox(records[0].instance_id)
            assert result.status is DisposalStatus.DISPOSED
            assert len(await client.list_sandboxes()) == 2
        assert list(tmp_path.iterdir()) == []

    asyncio.run(check())


def test_loopback_endpoint_shows_and_purges_a_conversation(tmp_path):
    async def check() -> None:
        async with SandboxControlServer(
            MemoryControl.demo(now=1_000),
            source_id="test-host",
            manifest_directory=tmp_path,
        ) as server:
            client = HttpControl(server.manifest)
            await HttpControl(EndpointManifest("manual", server.endpoint, None)).health()
            instance_id = "f2ecba87b2ce44659a66fd28fd0a1002"
            record = await client.get_sandbox(instance_id)
            assert record is not None
            assert record.thread_id == "forecast-042"

            purged = await client.purge_thread("tenant-labs", "forecast-042")
            assert purged.status is PurgeStatus.PURGED
            assert purged.disposed == 1
            assert await client.get_sandbox(instance_id) is None

    asyncio.run(check())


@pytest.mark.parametrize(
    "path",
    [
        "/v1/sandboxes?timeout=invalid",
        "/v1/sandboxes?timeout=1&timeout=2",
        "/v1/sandboxes/generation-a?timeout=invalid",
    ],
)
def test_loopback_get_refuses_malformed_timeouts(tmp_path, path: str):
    async def check() -> None:
        async with SandboxControlServer(
            MemoryControl.demo(now=1_000),
            source_id="test-host",
            manifest_directory=tmp_path,
        ) as server:
            with pytest.raises(ControlEndpointError) as raised:
                await asyncio.to_thread(HttpControl(server.manifest)._request, "GET", path)
            assert raised.value.status_code == 400

    asyncio.run(check())


def test_client_inventory_deadline_cancels_the_server_operation(tmp_path):
    class HangingControl(MemoryControl):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = asyncio.Event()

        async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            raise AssertionError("unreachable")

    async def check() -> None:
        control = HangingControl()
        server = SandboxControlServer(
            control,
            source_id="test-host",
            manifest_directory=tmp_path,
        )
        async with server:
            with pytest.raises(ControlEndpointError) as raised:
                await HttpControl(server.manifest, timeout=0.01).list_sandboxes()
            assert raised.value.status_code == 504
            await asyncio.wait_for(control.cancelled.wait(), timeout=1)

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["dispose", "purge"])
def test_client_delete_deadline_cancels_the_server_operation(tmp_path, operation: str):
    class HangingControl(MemoryControl):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = asyncio.Event()

        async def _hang(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        async def dispose_sandbox(
            self, instance_id: str, *, timeout: float = 10.0
        ) -> DisposalResult:
            del instance_id, timeout
            await self._hang()
            raise AssertionError("unreachable")

        async def purge_thread(
            self, scope: str, thread_id: str, *, timeout: float = 10.0
        ) -> PurgeResult:
            del scope, thread_id, timeout
            await self._hang()
            raise AssertionError("unreachable")

    async def check() -> None:
        control = HangingControl()
        async with SandboxControlServer(
            control,
            source_id="test-host",
            manifest_directory=tmp_path,
        ) as server:
            client = HttpControl(server.manifest)
            started = asyncio.get_running_loop().time()
            if operation == "dispose":
                result = await client.dispose_sandbox("generation-a", timeout=0.01)
                assert result.status is DisposalStatus.FAILED
            else:
                with pytest.raises(ControlEndpointError):
                    await client.purge_thread("scope", "thread", timeout=0.01)
            assert asyncio.get_running_loop().time() - started < 0.5
            await asyncio.wait_for(control.cancelled.wait(), timeout=0.5)

    asyncio.run(check())


def test_discovery_preserves_failed_health_probes_for_every_operation(tmp_path):
    stale = EndpointManifest("stopped-host", "http://127.0.0.1:1", 1)
    (tmp_path / "stopped.json").write_text(json.dumps(stale.to_json()), encoding="utf-8")

    async def check() -> None:
        async with SandboxControlServer(
            MemoryControl.demo(now=1_000),
            source_id="test-host",
            manifest_directory=tmp_path,
        ):
            control = await discover_controls(tmp_path)
            with pytest.raises(PartialInventoryError) as raised:
                await control.list_sandboxes()
            assert len(raised.value.records) == 3
            assert raised.value.errors[0].startswith("stopped-host:")

            purged = await control.purge_thread("tenant-labs", "forecast-042")
            assert purged.status is PurgeStatus.PARTIAL
            assert purged.disposed == 1

    asyncio.run(check())


def test_health_refuses_a_boolean_protocol_version():
    class BooleanVersionControl(HttpControl):
        def _request(self, method: str, path: str, *, timeout: float | None = None) -> object:
            del method, path, timeout
            return {"protocol_version": True, "source_id": "host"}

    manifest = EndpointManifest("host", "http://127.0.0.1:1", 1)
    with pytest.raises(ControlEndpointError, match="incompatible health"):
        asyncio.run(BooleanVersionControl(manifest).health())


def test_host_timeout_caps_a_shorter_client_purge_timeout(tmp_path):
    class RecordingControl(MemoryControl):
        def __init__(self) -> None:
            super().__init__()
            self.timeouts: list[float] = []

        async def purge_thread(
            self, scope: str, thread_id: str, *, timeout: float = 10.0
        ) -> PurgeResult:
            self.timeouts.append(timeout)
            return await super().purge_thread(scope, thread_id, timeout=timeout)

    async def check() -> None:
        control = RecordingControl()
        async with SandboxControlServer(
            control,
            source_id="test-host",
            manifest_directory=tmp_path,
            dispose_timeout=1.0,
        ) as server:
            client = HttpControl(server.manifest)
            await client.purge_thread("scope", "long-client", timeout=9.0)
            await client.purge_thread("scope", "short-client", timeout=0.25)
        assert control.timeouts == [1.0, 0.25]

    asyncio.run(check())


def test_constructing_server_does_not_open_a_port_or_publish_discovery(tmp_path):
    server = SandboxControlServer(
        MemoryControl.demo(),
        source_id="test-host",
        manifest_directory=tmp_path,
    )
    with pytest.raises(RuntimeError, match="not started"):
        _ = server.endpoint
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:9000",
        "http://example.com:9000",
        "http://127.0.0.1:9000/control",
        "http://127.0.0.1",
    ],
)
def test_endpoint_manifest_refuses_nonlocal_or_ambiguous_urls(endpoint):
    with pytest.raises(ValueError, match="HTTP loopback URL with a port"):
        EndpointManifest("invalid", endpoint, 42)


def test_endpoint_manifest_accepts_an_unknown_direct_endpoint_process():
    manifest = EndpointManifest("manual", "http://127.0.0.1:9000", None)

    assert EndpointManifest.from_json(manifest.to_json()) == manifest


@pytest.mark.parametrize("protocol_version", [2, True, 1.0, "1"])
def test_endpoint_manifest_constructor_refuses_an_unsupported_protocol(protocol_version):
    with pytest.raises(ValueError, match="unsupported control protocol version"):
        EndpointManifest(
            "invalid",
            "http://127.0.0.1:9000",
            None,
            protocol_version=protocol_version,
        )


@pytest.mark.parametrize("process_id", [True, "42"])
def test_endpoint_manifest_refuses_a_malformed_process_identity(process_id):
    with pytest.raises(ValueError, match="integer or null"):
        EndpointManifest("invalid", "http://127.0.0.1:9000", process_id)

    value = EndpointManifest("invalid", "http://127.0.0.1:9000", None).to_json()
    value["process_id"] = process_id
    with pytest.raises(ValueError, match="integer or null"):
        EndpointManifest.from_json(value)


def test_discovery_ignores_incompatible_and_malformed_files(tmp_path):
    (tmp_path / "invalid.json").write_text("{", encoding="utf-8")
    direct = EndpointManifest("manual", "http://127.0.0.1:1", None).to_json()
    (tmp_path / "direct.json").write_text(json.dumps(direct), encoding="utf-8")
    incompatible = EndpointManifest("old", "http://127.0.0.1:1", 42).to_json()
    incompatible["protocol_version"] = 2
    (tmp_path / "old.json").write_text(json.dumps(incompatible), encoding="utf-8")
    incompatible["protocol_version"] = True
    (tmp_path / "boolean.json").write_text(json.dumps(incompatible), encoding="utf-8")
    assert read_manifests(tmp_path) == ()


def test_windows_runtime_fallback_is_stable_per_user(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(getpass, "getuser", lambda: "domain\\operator")

    first = server_module._windows_runtime_directory()
    assert server_module._windows_runtime_directory() == first
    assert first.parent == tmp_path

    monkeypatch.setattr(getpass, "getuser", lambda: "domain\\other")
    assert server_module._windows_runtime_directory() != first
