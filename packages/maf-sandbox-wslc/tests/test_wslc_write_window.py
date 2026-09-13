"""Deterministic characterization of the WSLC check/copy boundary, including its residual.

Live measurements require MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE. They deliberately place a
swap at the boundary; they are not probabilistic race controls or proof of atomicity.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import replace

import pytest
from maf_sandbox import Capability, EntryKind, SandboxEntry, SandboxKey, SandboxSpec

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _WslcResult, _WslcSandbox

_WORK = "/maf-sandbox/work"
_PARENT = _WORK + "/parent"
_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE")
_SPEC = SandboxSpec(kind="write-window", image="fixture", requires=frozenset({Capability.FILES_IN}))


async def _operate(sandbox, operation, missing):
    if operation == "prepare":
        await sandbox.prepare_work_dir(replace(_SPEC, work_dir=_PARENT + "/child/base"))
    else:
        await sandbox.write_file(
            "parent/child/landed" if missing else "parent/landed",
            b"boundary payload",
            working_directory=_WORK,
        )


@pytest.mark.parametrize("operation", ["write", "prepare"])
@pytest.mark.parametrize("outcome", ["refuse", "cancel-check", "cancel-copy", "copy-error"])
def test_refusal_and_cancellation_at_the_write_boundary(operation, outcome):
    async def scenario():
        copying = asyncio.Event()
        checking = asyncio.Event()
        copies = 0

        async def run(*args, **kwargs):
            nonlocal copies
            assert args[:3] == ("container", "cp", "-")
            copies += 1
            copying.set()
            if outcome == "cancel-copy":
                await asyncio.Event().wait()
            return _WslcResult(1, b"", b"extraction refused")

        sandbox = _WslcSandbox(run, "fixture", 30, 10001, (10001, 20001), instance_id="id")

        async def stat(guest, rel):
            if guest == _PARENT:
                checking.set()
                if outcome == "cancel-check":
                    await asyncio.Event().wait()
                if outcome == "refuse":
                    return SandboxEntry(path=rel, kind=EntryKind.SYMLINK, size_bytes=None)
            if guest.startswith(_PARENT + "/"):
                return None
            return SandboxEntry(path=rel, kind=EntryKind.DIRECTORY, size_bytes=None)

        sandbox._stat_guest = stat
        task = asyncio.create_task(_operate(sandbox, operation, True))
        if outcome.startswith("cancel"):
            await asyncio.wait_for(
                copying.wait() if outcome == "cancel-copy" else checking.wait(), 5
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ValueError if outcome == "refuse" else RuntimeError):
                await task
        assert copies == (1 if outcome in ("cancel-copy", "copy-error") else 0)

    asyncio.run(scenario())


@pytest.mark.skipif(
    shutil.which("wslc") is None or not _IMAGE,
    reason="needs wslc and MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE",
)
@pytest.mark.parametrize(
    ("operation", "missing", "swap_missing_parent"),
    [
        ("write", False, False),
        ("write", True, False),
        ("write", True, True),
        ("prepare", True, False),
        ("prepare", True, True),
    ],
)
@pytest.mark.parametrize("boundary", ["swap", "refuse", "cancel"])
def test_live_write_window(operation, missing, swap_missing_parent, boundary):
    async def scenario():
        backend = WslcSandboxBackend(WslcSandboxConfig())
        key = SandboxKey(
            scope="write-window-" + uuid.uuid4().hex, thread_id="test", agent_id="test"
        )
        spec = replace(_SPEC, image=_IMAGE)
        try:
            sandbox = await backend.acquire(key, spec)
            run = backend._wslc

            async def command(script, *, root=False):
                args = ["container", "exec"]
                if root:
                    args += ["--user", "0"]
                return await run(
                    *args, "-w", "/", sandbox.container_name, "sh", "-c", script, timeout=30
                )

            identity = await command("id -u; id -g")
            assert identity.returncode == 0 and identity.stdout_text.splitlines() == [
                "10001",
                "20001",
            ]
            setup = await command(
                "mkdir /protected; chmod 700 /protected; "
                f"mkdir {_PARENT}; chown 10001:20001 {_PARENT}",
                root=True,
            )
            assert setup.returncode == 0, setup.stderr_text
            denied = await command("printf denied > /protected/control")
            assert denied.returncode != 0 and "Permission denied" in denied.stderr_text
            if swap_missing_parent:
                removed = await command(f"rmdir {_PARENT}")
                assert removed.returncode == 0

            async def swap():
                move = "" if swap_missing_parent else f"mv {_PARENT} {_WORK}/saved && "
                result = await command(move + f"ln -s /protected {_PARENT}")
                assert result.returncode == 0, result.stderr_text

            if boundary == "refuse":
                await swap()
            reached_copy = asyncio.Event()
            copies = 0

            async def intercept(*args, **kwargs):
                nonlocal copies
                if args[:3] == ("container", "cp", "-"):
                    copies += 1
                    reached_copy.set()
                    if boundary == "cancel":
                        await asyncio.Event().wait()
                    elif boundary == "swap":
                        await swap()
                return await run(*args, **kwargs)

            sandbox._run = intercept
            task = asyncio.create_task(_operate(sandbox, operation, missing))
            if boundary == "cancel":
                await asyncio.wait_for(reached_copy.wait(), 10)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif boundary == "refuse":
                with pytest.raises(ValueError):
                    await task
            else:
                await task
            sandbox._run = run
            assert copies == (0 if boundary == "refuse" else 1)

            protected = await command("find /protected -mindepth 1 -print", root=True)
            assert protected.returncode == 0
            escaped = boundary == "swap" and not swap_missing_parent
            if escaped:
                suffix = (
                    "/child/base"
                    if operation == "prepare"
                    else ("/child/landed" if missing else "/landed")
                )
                target = "/protected" + suffix
                assert target in protected.stdout_text.splitlines()
                metadata = await command(
                    f"stat -c '%u:%g:%a' {target}; stat -c '%u:%g:%a' /protected", root=True
                )
                assert metadata.stdout_text.splitlines() == [
                    "10001:20001:" + ("755" if operation == "prepare" else "644"),
                    "0:0:700",
                ]
                if operation == "write":
                    content = await command(f"cat {target}", root=True)
                    assert content.stdout_text == "boundary payload"
                else:
                    ancestor = await command("stat -c '%u:%g' /protected/child", root=True)
                    assert ancestor.stdout_text.strip() == "0:0"
            else:
                assert protected.stdout_text == ""
            if boundary == "swap" and swap_missing_parent:
                replaced = await command(f"test -d {_PARENT} && test ! -L {_PARENT}")
                assert replaced.returncode == 0

            # Cancellation before submission leaves the container usable and nothing to thaw.
            usable = await command("printf usable")
            assert usable.returncode == 0 and usable.stdout_text == "usable"
        finally:
            assert await backend.dispose(key, kind=spec.kind) is None

    asyncio.run(scenario())
