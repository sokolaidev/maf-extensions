"""The WSLC check/placement boundary, and how far a swap there reaches.

Writes run as the image's user, so a swapped parent redirects them only to where that user
can write. Working-directory setup runs as root inside directories its shell holds, so a
parent replaced by a **link** is refused — that is what these tests plant. A real directory
renamed into the same name is not detected and still receives root's `mkdir` and `chown`;
that residual is stated in the backend contract and is not covered here. Live measurements
require MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE. They place the swap at the boundary
deliberately; they are not probabilistic race controls.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import shutil
import uuid
from dataclasses import replace

import pytest
from maf_sandbox import Capability, EntryKind, SandboxEntry, SandboxKey, SandboxSpec

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import (
    _CREATE_DIRECTORIES,
    _WRITE_AS_THE_GUEST,
    _WslcResult,
    _WslcSandbox,
)

_WORK = "/maf-sandbox/work"
_PARENT = _WORK + "/parent"
_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE")
_SPEC = SandboxSpec(kind="write-window", image="fixture", requires=frozenset({Capability.FILES_IN}))


def _places(args: tuple[str, ...]) -> bool:
    """Whether a wslc command is the one that places a write or creates a directory."""
    return _WRITE_AS_THE_GUEST in args or _CREATE_DIRECTORIES in args


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
@pytest.mark.parametrize(
    "outcome", ["refuse", "cancel-check", "cancel-placement", "placement-error"]
)
def test_refusal_and_cancellation_at_the_write_boundary(operation, outcome):
    async def scenario():
        placing = asyncio.Event()
        checking = asyncio.Event()
        placements = 0

        async def run(*args, **kwargs):
            nonlocal placements
            assert _places(args)
            placements += 1
            placing.set()
            if outcome == "cancel-placement":
                await asyncio.Event().wait()
            return _WslcResult(1, b"", b"placement refused")

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
                placing.wait() if outcome == "cancel-placement" else checking.wait(), 5
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ValueError if outcome == "refuse" else RuntimeError):
                await task
        assert placements == (1 if outcome in ("cancel-placement", "placement-error") else 0)

    asyncio.run(scenario())


class _Live:
    """A fresh guest-owned container with a root-only ``/protected`` and a guest-owned parent."""

    def __init__(self) -> None:
        self.backend = WslcSandboxBackend(WslcSandboxConfig())
        self.key = SandboxKey(
            scope="write-window-" + uuid.uuid4().hex, thread_id="test", agent_id="test"
        )
        self.spec = replace(_SPEC, image=_IMAGE)
        self.run = self.backend._wslc

    async def open(self, spec: SandboxSpec | None = None):
        self.sandbox = await self.backend.acquire(self.key, spec or self.spec)
        identity = await self.command("id -u; id -g")
        assert identity.returncode == 0 and identity.stdout_text.splitlines() == [
            "10001",
            "20001",
        ]
        setup = await self.command(
            "mkdir -p /protected; chmod 700 /protected; "
            f"mkdir -p {_PARENT}; chown 10001:20001 {_PARENT}",
            root=True,
        )
        assert setup.returncode == 0, setup.stderr_text
        denied = await self.command("printf denied > /protected/control")
        assert denied.returncode != 0 and "Permission denied" in denied.stderr_text
        return self.sandbox

    async def command(self, script, *, root=False):
        args = ["container", "exec"]
        if root:
            args += ["--user", "0"]
        return await self.run(
            *args, "-w", "/", self.sandbox.container_name, "sh", "-c", script, timeout=30
        )

    async def protected(self) -> list[str]:
        listed = await self.command("find /protected -mindepth 1 -print", root=True)
        assert listed.returncode == 0
        return listed.stdout_text.splitlines()

    async def close(self) -> None:
        assert await self.backend.dispose(self.key, kind=self.spec.kind) is None


_LIVE = pytest.mark.skipif(
    shutil.which("wslc") is None or not _IMAGE,
    reason="needs wslc and MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE",
)


@_LIVE
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
        live = _Live()
        try:
            sandbox = await live.open()
            if swap_missing_parent:
                removed = await live.command(f"rmdir {_PARENT}")
                assert removed.returncode == 0

            async def swap():
                move = "" if swap_missing_parent else f"mv {_PARENT} {_WORK}/saved && "
                result = await live.command(move + f"ln -s /protected {_PARENT}")
                assert result.returncode == 0, result.stderr_text

            if boundary == "refuse":
                await swap()
            reached = asyncio.Event()
            placements = 0

            async def intercept(*args, **kwargs):
                nonlocal placements
                if _places(args):
                    placements += 1
                    reached.set()
                    if boundary == "cancel":
                        await asyncio.Event().wait()
                    elif boundary == "swap":
                        await swap()
                return await live.run(*args, **kwargs)

            sandbox._run = intercept
            task = asyncio.create_task(_operate(sandbox, operation, missing))
            if boundary == "cancel":
                await asyncio.wait_for(reached.wait(), 10)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif boundary == "refuse":
                with pytest.raises(ValueError):
                    await task
            else:
                # The guest's own permission refuses a write; held setup refuses the swap.
                with pytest.raises(PermissionError if operation == "write" else RuntimeError):
                    await task
            sandbox._run = live.run
            assert placements == (0 if boundary == "refuse" else 1)
            assert await live.protected() == []
            if boundary == "swap" and swap_missing_parent:
                # Nothing replaced the planted link either: it is still the guest's.
                kept = await live.command(f"test -L {_PARENT}")
                assert kept.returncode == 0

            # A cancelled or refused placement leaves the container usable.
            usable = await live.command("printf usable")
            assert usable.returncode == 0 and usable.stdout_text == "usable"
        finally:
            await live.close()

    asyncio.run(scenario())


@_LIVE
@pytest.mark.parametrize(
    "operation", ["write", "write-missing-parents", "prepare", "write-root-owned"]
)
def test_live_placement_without_a_swap(operation):
    """The control: with nothing swapped, each operation lands where it was asked.

    Writes belong to the image's user because that user wrote them. Setup leaves the new
    intermediate directory to root and gives the base to the image's user. A write where that
    user cannot write is refused, even though root could have placed it. The fixture's base
    is setgid, so a directory created beneath it takes its group and the bit. The base is
    then chowned, which overwrites that group with the image user's; the fixture cannot show
    the difference because both are 20001, so a separate control uses a parent whose group
    differs.
    """

    async def scenario():
        live = _Live()
        try:
            sandbox = await live.open()
            if operation == "prepare":
                await sandbox.prepare_work_dir(replace(_SPEC, work_dir=_PARENT + "/child/base"))
                paths = [f"{_PARENT}/child", f"{_PARENT}/child/base"]
                expected = ["0:20001:2755", "10001:20001:2755"]
            elif operation == "write-root-owned":
                with pytest.raises(PermissionError):
                    await sandbox.write_file("landed", b"payload", working_directory="/etc")
                absent = await live.command("test ! -e /etc/landed", root=True)
                assert absent.returncode == 0
                return
            else:
                name = "child/landed" if operation == "write-missing-parents" else "landed"
                await sandbox.write_file(
                    f"parent/{name}", b"boundary payload", working_directory=_WORK
                )
                paths = [f"{_PARENT}/{name}"]
                expected = ["10001:20001:644"]
                if operation == "write-missing-parents":
                    paths.insert(0, f"{_PARENT}/child")
                    expected.insert(0, "10001:20001:2755")
                content = await live.command(f"cat {paths[-1]}")
                assert content.stdout_text == "boundary payload"
                leftovers = await live.command(f"ls -A {posixpath.dirname(paths[-1])}")
                assert leftovers.stdout_text.split() == ["landed"]
            metadata = await live.command(f"stat -c '%u:%g:%a' {' '.join(paths)}", root=True)
            assert metadata.stdout_text.splitlines() == expected
        finally:
            await live.close()

    asyncio.run(scenario())


@_LIVE
def test_live_warm_setup_refuses_a_parent_swapped_after_the_check():
    """A warm acquire that recreates a missing base holds its parent the same way.

    The base sits in a directory the guest replaced with its own, so the guest can swap that
    directory for a link between the check and the setup command.
    """

    async def scenario():
        live = _Live()
        spec = replace(live.spec, work_dir=f"{_WORK}/outer/base")
        try:
            await live.open(spec)
            replaced = await live.command(
                f"mv {_WORK}/outer {_WORK}/outer.saved && mkdir {_WORK}/outer"
            )
            assert replaced.returncode == 0, replaced.stderr_text

            # A refused setup disposes the container, so read the protected directory while
            # it is still there: right after the setup command answered.
            landed: list[str] = []

            async def intercept(*args, **kwargs):
                if _CREATE_DIRECTORIES not in args:
                    return await live.run(*args, **kwargs)
                swapped = await live.command(
                    f"mv {_WORK}/outer {_WORK}/outer.mine && ln -s /protected {_WORK}/outer"
                )
                assert swapped.returncode == 0, swapped.stderr_text
                result = await live.run(*args, **kwargs)
                landed.extend(await live.protected())
                return result

            live.backend._wslc = intercept
            with pytest.raises(RuntimeError, match="does not resolve to itself any more"):
                await live.backend.acquire(live.key, spec)
            live.backend._wslc = live.run
            assert landed == []
        finally:
            live.backend._wslc = live.run
            await live.close()

    asyncio.run(scenario())


@_LIVE
def test_live_setup_refuses_a_directory_swapped_right_after_mkdir():
    """The check after each ``mkdir``: a new directory replaced by a link is not entered.

    A wrapper stands in for ``mkdir`` in this one container and swaps the directory it just
    made, which puts the swap between creation and the ``cd -P`` that holds it.
    """

    async def scenario():
        live = _Live()
        try:
            await live.open()
            wrapped = await live.command(
                "mv /usr/bin/mkdir /usr/bin/mkdir.real && printf '%s\n' '#!/bin/sh' "
                "'/usr/bin/mkdir.real \"$@\" || exit' 'for last; do :; done' "
                "'[ \"$last\" = child ] && mv child child.moved && ln -s /protected child' "
                "'exit 0' > /usr/bin/mkdir && chmod 755 /usr/bin/mkdir",
                root=True,
            )
            assert wrapped.returncode == 0, wrapped.stderr_text
            with pytest.raises(RuntimeError, match="does not resolve to itself any more"):
                await live.sandbox.prepare_work_dir(
                    replace(_SPEC, work_dir=_PARENT + "/child/base")
                )
            assert await live.protected() == []
        finally:
            await live.close()

    asyncio.run(scenario())


@_LIVE
def test_live_cancelling_after_the_guest_command_started_is_not_a_rollback():
    """The documented after-start contract, with the command provably running.

    The other cancellation case stops before anything is submitted. This one waits until the
    guest has written the staged sibling, so what it measures is the half the contract
    describes: the command outlives the host, the target does not appear while it is blocked,
    and the container stays usable.
    """

    async def scenario():
        live = _Live()
        try:
            sandbox = await live.open()
            # `mv` blocks, so the command is stuck after `cat` wrote the staged sibling.
            shimmed = await live.command(
                "mv /usr/bin/mv /usr/bin/mv.real; printf '%s\n' '#!/bin/sh' "
                "'exec sleep 3600' > /usr/bin/mv; chmod 755 /usr/bin/mv",
                root=True,
            )
            assert shimmed.returncode == 0, shimmed.stderr_text
            task = asyncio.create_task(
                sandbox.write_file("cancelled.txt", b"payload", working_directory=_WORK)
            )
            for _ in range(100):
                await asyncio.sleep(0.1)
                staged = await live.command(f"ls -A {_WORK}")
                if ".maf-" in staged.stdout_text:
                    break
            else:  # pragma: no cover - the guest never got as far as staging
                task.cancel()
                raise AssertionError("the guest command never wrote its staged sibling")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            restored = await live.command("cp /usr/bin/mv.real /usr/bin/mv", root=True)
            assert restored.returncode == 0, restored.stderr_text
            # Not a rollback: the sibling is still there, and the target never appeared while
            # the command was blocked on it.
            left = await live.command(f"ls -A {_WORK}")
            assert ".maf-" in left.stdout_text
            assert "cancelled.txt" not in left.stdout_text.split()
            usable = await live.command("printf usable")
            assert usable.returncode == 0 and usable.stdout_text == "usable"
        finally:
            await live.close()

    asyncio.run(scenario())


@_LIVE
def test_live_a_created_base_takes_the_image_users_group_not_the_inherited_one():
    """The control the fixture cannot be: a setgid parent whose group is not the image user's.

    `mkdir` gives a new directory its parent's group, and the ownership step then sets
    `uid:gid` outright — so the base ends up with the image user's group, not the inherited
    one. The setgid bit survives, because chown clears it only for non-directories.
    """

    async def scenario():
        live = _Live()
        spec = replace(live.spec, work_dir=f"{_WORK}/inherited/base")
        try:
            await live.open(spec)
            # Root's group, deliberately not the image user's 20001, and setgid so a child
            # would inherit it if nothing overwrote it.
            staged = await live.command(
                f"rm -rf {_WORK}/inherited; mkdir {_WORK}/inherited; "
                f"chgrp 0 {_WORK}/inherited; chmod 2775 {_WORK}/inherited",
                root=True,
            )
            assert staged.returncode == 0, staged.stderr_text
            sandbox = await live.backend.acquire(live.key, spec)
            assert sandbox is not None
            metadata = await live.command(
                f"stat -c '%u:%g:%a' {_WORK}/inherited {_WORK}/inherited/base", root=True
            )
            parent, base = metadata.stdout_text.splitlines()
            assert parent == "0:0:2775", parent
            # Group 20001 is the image user's, not the 0 it would have inherited.
            assert base == "10001:20001:2755", base
        finally:
            await live.close()

    asyncio.run(scenario())
