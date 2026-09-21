"""Live root-probe checks with a guest-writable directory ahead of /usr/bin in PATH."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import uuid
from dataclasses import replace

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxSpec

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _WslcResult

_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_PATH_SHADOW_IMAGE")
_WORK = "/maf-sandbox/work"
_MARKER = "/root/maf-path-probe"

pytestmark = pytest.mark.skipif(
    shutil.which("wslc") is None or not _IMAGE,
    reason="needs wslc and MAF_SANDBOX_WSLC_E2E_PATH_SHADOW_IMAGE from fixtures/path-shadow",
)


@pytest.mark.parametrize("cached", [False, True])
def test_root_probes_bypass_a_guest_supplied_test(cached):
    async def scenario():
        backend = WslcSandboxBackend(WslcSandboxConfig())
        key = SandboxKey(scope="root-probe-" + uuid.uuid4().hex, thread_id="test", agent_id="test")
        spec = SandboxSpec(kind="root-probe", image=_IMAGE, requires=frozenset({Capability.EXEC}))
        file_spec = replace(spec, requires=frozenset({Capability.EXEC, Capability.FILES_IN}))
        try:
            sandbox = await backend.acquire(key, file_spec if cached else spec)

            async def root(*argv: str) -> _WslcResult:
                return await backend._wslc(
                    "container",
                    "exec",
                    "--user",
                    "0",
                    "-w",
                    "/",
                    sandbox.container_name,
                    *argv,
                    timeout=30,
                )

            async def guest(script: str) -> str:
                result = await sandbox.exec(script, working_directory=_WORK, timeout=30)
                assert result.exit_code == 0, result.stderr
                return result.stdout

            assert (await guest("/usr/bin/id -u; /usr/bin/id -g")).splitlines() == [
                "10001",
                "20001",
            ]
            denied = await sandbox.exec(
                f"printf denied > {_MARKER}", working_directory=_WORK, timeout=30
            )
            assert denied.exit_code != 0 and "Permission denied" in denied.stderr

            payload = f'#!/bin/sh\n/usr/bin/id -u >> {_MARKER}\nexec /usr/bin/test "$@"\n'
            await guest(
                f"printf %s {shlex.quote(payload)} > /usr/local/bin/test && "
                "/usr/bin/chmod 755 /usr/local/bin/test"
            )
            # The control proves that this fixture executes the guest's file as root.
            control = await root("test", "-d", "/")
            assert control.returncode == 0, control.stderr_text
            marker = await root("/usr/bin/cat", _MARKER)
            assert marker.returncode == 0 and marker.stdout_text.strip() == "0"
            removed = await root("/usr/bin/rm", "--", _MARKER)
            assert removed.returncode == 0

            sandbox = await backend.acquire(key, file_spec)
            marker = await root("/usr/bin/test", "-e", _MARKER)
            assert marker.returncode == 1, "acquisition ran the guest's test as root"

            path = "a 'quoted'; $(touch injected) file"
            await guest(
                f"printf original > {shlex.quote(path)} && "
                f"ln -s {shlex.quote(path)} linked && ln -s missing dangling"
            )
            await sandbox.write_file(path, b"replacement", working_directory=_WORK)
            assert await guest(f"cat -- {shlex.quote(path)}") == "replacement"
            for link in ("linked", "dangling"):
                with pytest.raises(ValueError):
                    await sandbox.write_file(link, b"refused", working_directory=_WORK)
            marker = await root("/usr/bin/test", "-e", _MARKER)
            assert marker.returncode == 1, "path classification ran the guest's test as root"
            await guest("/usr/bin/test ! -e injected")
        finally:
            assert await backend.dispose(key, kind=spec.kind) is None

    asyncio.run(scenario())
