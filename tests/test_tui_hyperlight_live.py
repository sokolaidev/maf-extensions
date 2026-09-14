"""Opt-in MST command checks against real Hyperlight workers on Windows WHP."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxRouter, SandboxSpec
from maf_sandbox_hyperlight import HyperlightSandboxBackend
from maf_sandbox_tui import HyperlightControl, MonitoredSandboxBackend, SandboxControlServer

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_HYPERLIGHT_LIVE") != "1" or sys.platform != "win32",
    reason="requires opt-in and Windows WHP",
)

_SPEC = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))


async def _mst(*arguments: str) -> object:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "maf_sandbox_tui",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    return json.loads(stdout)


def test_every_mst_command_against_real_hyperlight_workers(tmp_path: Path):
    async def check() -> None:
        backend = HyperlightSandboxBackend()
        monitored = MonitoredSandboxBackend(backend)
        router = SandboxRouter([monitored])
        first_key = SandboxKey("mst-live", "delete", "worker")
        second_key = SandboxKey("mst-live", "purge", "worker")
        try:
            first = await router.acquire(first_key, _SPEC)
            second = await router.acquire(second_key, _SPEC)
            first_result = await first.run_code("print(6 * 7)", timeout=5)
            second_result = await second.run_code("print(7 * 8)", timeout=5)
            assert first_result.stdout == "42\n"
            assert second_result.stdout == "56\n"

            control = HyperlightControl(monitored, router, source_id="hyperlight-live")
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
