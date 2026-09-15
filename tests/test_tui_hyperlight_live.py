"""Opt-in MST command checks against real Hyperlight workers on Windows WHP."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version
from pathlib import Path

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxSpec
from maf_sandbox_hyperlight import HyperlightSandboxBackend
from maf_sandbox_tui import (
    HyperlightControl,
    MonitoredSandboxBackend,
    MonitoredSandboxRouter,
    SandboxControlServer,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_HYPERLIGHT_LIVE") != "1" or sys.platform != "win32",
    reason="requires opt-in and Windows WHP",
)

_SPEC = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))
_MST_COMMAND_TIMEOUT = 30.0


async def _mst(*arguments: str) -> object:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "maf_sandbox_tui",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=_MST_COMMAND_TIMEOUT)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.communicate()
        raise
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    return json.loads(stdout)


def test_mst_timeout_kills_and_reaps_the_child(monkeypatch):
    async def check() -> None:
        original_exec = asyncio.create_subprocess_exec
        processes: list[asyncio.subprocess.Process] = []

        async def slow_exec(*_args: str, **_kwargs: object) -> asyncio.subprocess.Process:
            process = await original_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_exec)
        monkeypatch.setattr(sys.modules[__name__], "_MST_COMMAND_TIMEOUT", 0.01)
        with pytest.raises(TimeoutError):
            await _mst("version", "--json")
        assert len(processes) == 1
        assert processes[0].returncode is not None

    asyncio.run(check())


def test_every_mst_command_against_real_hyperlight_workers(tmp_path: Path):
    async def check() -> None:
        backend = HyperlightSandboxBackend()
        monitored = MonitoredSandboxBackend(backend)
        router = MonitoredSandboxRouter([monitored])
        lifecycle_gate = asyncio.Lock()

        async def acquire(key: SandboxKey):
            async with lifecycle_gate:
                return await router.acquire(key, _SPEC)

        async def purge(scope: str, thread_id: str):
            async with lifecycle_gate:
                return await router.dispose_scope(scope, thread_id)

        @asynccontextmanager
        async def quiesce_instance(key: SandboxKey) -> AsyncIterator[None]:
            assert key.scope == "mst-live"
            async with lifecycle_gate:
                yield

        first_key = SandboxKey("mst-live", "delete", "worker")
        second_key = SandboxKey("mst-live", "purge", "worker")
        try:
            first = await acquire(first_key)
            second = await acquire(second_key)
            async with lifecycle_gate:
                first_result = await first.run_code("print(6 * 7)", timeout=5)
            async with lifecycle_gate:
                second_result = await second.run_code("print(7 * 8)", timeout=5)
            assert first_result.stdout == "42\n"
            assert second_result.stdout == "56\n"

            control = HyperlightControl(
                monitored,
                router,
                source_id="hyperlight-live",
                quiesce_instance=quiesce_instance,
                quiesced_purge=purge,
            )
            async with SandboxControlServer(
                control,
                source_id="hyperlight-live",
                manifest_directory=tmp_path,
            ) as server:
                connection = ("--endpoint", server.endpoint, "--source", "hyperlight-live")

                reported_version = await _mst("version", "--json")
                assert isinstance(reported_version, dict)
                assert reported_version["version"] == version("maf-sandbox-tui")

                update = await _mst(
                    "update",
                    "--to",
                    version("maf-sandbox-tui"),
                    "--json",
                )
                assert isinstance(update, dict)
                assert update["status"] == "current"

                hosts = await _mst("hosts", *connection, "--json")
                assert isinstance(hosts, list)
                assert hosts[0]["status"] == "healthy"

                records = await _mst("list", *connection, "--json")
                assert isinstance(records, list)
                assert {item["instance_id"] for item in records} == {
                    first.instance_id,
                    second.instance_id,
                }

                shown = await _mst("show", first.instance_id, *connection, "--json")
                assert isinstance(shown, dict)
                assert shown["instance_id"] == first.instance_id

                watched = await _mst(
                    "watch",
                    *connection,
                    "--count",
                    "1",
                    "--jsonl",
                )
                assert isinstance(watched, dict)
                assert len(watched["sandboxes"]) == 2

                deleted = await _mst(
                    "delete",
                    first.instance_id,
                    *connection,
                    "--yes",
                    "--json",
                )
                assert isinstance(deleted, dict)
                assert deleted["status"] == "disposed"

                purged = await _mst(
                    "purge-thread",
                    *connection,
                    "--scope",
                    second_key.scope,
                    "--thread",
                    second_key.thread_id,
                    "--yes",
                    "--json",
                )
                assert isinstance(purged, dict)
                assert purged["status"] == "purged"
                assert purged["disposed"] == 1
                assert await monitored.list_sandboxes() == ()
        finally:
            await backend.aclose()

    asyncio.run(check())
