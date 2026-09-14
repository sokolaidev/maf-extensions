"""Control records, local discovery and exact-instance disposal."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from typing import cast

import pytest
from maf_sandbox import SandboxKey, SandboxRouter, ScopePurge

from maf_sandbox_tui import (
    CompositeControl,
    DisposalStatus,
    EndpointManifest,
    HttpControl,
    HyperlightControl,
    MemoryControl,
    PurgeResult,
    PurgeStatus,
    SandboxControlServer,
    SandboxRecord,
    SandboxState,
)
from maf_sandbox_tui._client import read_manifests


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

    async def list_sandboxes(self) -> tuple[_Info, ...]:
        return tuple(self.records)


class _Router:
    def __init__(self, inventory: _Inventory, replacement: _Info) -> None:
        self.inventory = inventory
        self.replacement = replacement
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
        self.inventory.records = [self.replacement]
        return True

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        self.scope_calls.append((scope, thread_id))
        before = len(self.inventory.records)
        self.inventory.records = [
            item
            for item in self.inventory.records
            if (item.key.scope, item.key.thread_id) != (scope, thread_id)
        ]
        return ScopePurge(before - len(self.inventory.records))


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


def test_hyperlight_control_routes_exact_generation_and_preserves_replacement():
    async def check() -> None:
        key = SandboxKey("tenant-labs", "thread-1", "agent-1")
        target = _Info(key, "codeact", "generation-a")
        replacement = replace(target, instance_id="generation-b")
        inventory = _Inventory([target])
        router = _Router(inventory, replacement)
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        listed = await control.list_sandboxes()
        assert listed[0].source_id == "agent-app"
        assert listed[0].logical_name == "tenant-labs/thread-1/agent-1"
        result = await control.dispose_sandbox(target.instance_id, timeout=3.0)

        assert result.status is DisposalStatus.DISPOSED
        assert router.calls == [(key, "codeact", "generation-a", 3.0)]
        assert [item.instance_id for item in await inventory.list_sandboxes()] == ["generation-b"]

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
        control = HyperlightControl(inventory, cast(SandboxRouter, router), source_id="agent-app")

        result = await control.purge_thread("tenant-labs", "thread-1")

        assert result.status is PurgeStatus.PURGED
        assert result.disposed == 1
        assert router.scope_calls == [("tenant-labs", "thread-1")]
        assert [item.instance_id for item in inventory.records] == ["generation-b"]

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
            await HttpControl(EndpointManifest("manual", server.endpoint, 0)).health()
            instance_id = "f2ecba87b2ce44659a66fd28fd0a1002"
            record = await client.get_sandbox(instance_id)
            assert record is not None
            assert record.thread_id == "forecast-042"

            purged = await client.purge_thread("tenant-labs", "forecast-042")
            assert purged.status is PurgeStatus.PURGED
            assert purged.disposed == 1
            assert await client.get_sandbox(instance_id) is None

    asyncio.run(check())


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


def test_discovery_ignores_incompatible_and_malformed_files(tmp_path):
    (tmp_path / "invalid.json").write_text("{", encoding="utf-8")
    incompatible = EndpointManifest("old", "http://127.0.0.1:1", 42).to_json()
    incompatible["protocol_version"] = 2
    (tmp_path / "old.json").write_text(json.dumps(incompatible), encoding="utf-8")
    assert read_manifests(tmp_path) == ()
