"""Live tests against a real ``wslc`` and a real container image.

Skipped unless ``wslc`` is on ``PATH`` and ``MAF_SANDBOX_WSLC_E2E_IMAGE`` names an image to
run — the offline suite already pins every command line, and what is left to prove is that
those commands do what this backend believes when a real container is on the other end.
The image is read from the environment rather than written down here so a local tag never
becomes a committed one; any Linux image with ``sh`` and ``sleep`` will do.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid

import pytest
from maf_sandbox import SandboxKey, SandboxSpec

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_IMAGE")

pytestmark = pytest.mark.skipif(
    shutil.which("wslc") is None or not _IMAGE,
    reason="needs wslc on PATH and MAF_SANDBOX_WSLC_E2E_IMAGE naming a runnable image",
)


def _spec() -> SandboxSpec:
    return SandboxSpec(kind="e2e", image=_IMAGE)


def _key(scope: str) -> SandboxKey:
    return SandboxKey(scope=scope, thread_id="thread-1", agent_dir="devops-engineer")


def _names_on_the_machine(name: str) -> list[str]:
    """Every container currently named ``name``, read with wslc rather than the backend.

    Read as JSON: the table view truncates the NAME column, so scanning it can miss a
    container that is still there and pass an emptiness assertion that should fail.
    """
    listing = subprocess.run(
        ["wslc", "container", "list", "-a", "--format", "json", "--filter", f"name={name}"],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    rows = json.loads(listing) if listing.strip() else []
    return [row["Name"] for row in rows if row.get("Name") == name]


class TestALiveContainer:
    def test_a_file_written_survives_into_exec_and_the_container_is_reused(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file("/acas/work/nested/deep/main.bicep", "param naïve string\n")

            read_back = await sandbox.exec(
                ["cat", "nested/deep/main.bicep"], working_directory="/acas/work", timeout=60
            )
            assert read_back.exit_code == 0, read_back.stderr
            assert read_back.stdout == "param naïve string\n"

            failing = await sandbox.exec("exit 7", working_directory="/acas/work", timeout=60)
            assert failing.exit_code == 7

            warm = await backend.acquire(_key(scope), _spec())
            assert warm.container_name == sandbox.container_name
            still_there = await warm.exec(
                ["cat", "nested/deep/main.bicep"], working_directory="/acas/work", timeout=60
            )
            assert still_there.stdout == "param naïve string\n"

            await backend.dispose(_key(scope))
            assert _names_on_the_machine(sandbox.container_name) == []

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_dispose_scope_finds_the_container_by_its_labels(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> str:
            sandbox = await backend.acquire(_key(scope), _spec())
            # Purged through a second backend: the labels on the container, not this
            # process's memory, are what a conversation delete has to find.
            purged = await WslcSandboxBackend(WslcSandboxConfig()).dispose_scope(scope, "thread-1")
            assert purged >= 1
            return sandbox.container_name

        try:
            name = asyncio.run(scenario())
            assert _names_on_the_machine(name) == []
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))
