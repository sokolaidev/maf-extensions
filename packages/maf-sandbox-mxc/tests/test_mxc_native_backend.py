"""Opt-in native file conformance, cancellation and recoverable result replay."""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from maf_sandbox import ProgramRequirements, Sandbox, SandboxKey, SandboxSpec
from maf_sandbox.native_conformance import (
    assert_call_scope_conformance,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
    assert_reclaim_conformance,
)

from maf_sandbox_mxc._backend import MxcSandboxBackend
from maf_sandbox_mxc._config import MxcSandboxConfig, startup_identity
from maf_sandbox_mxc.host_call import digest
from maf_sandbox_mxc.shared_store import Limits, ScratchLimits

pytestmark = pytest.mark.skipif(
    not os.environ.get("MXC_ADAPTER_HELPER"), reason="native MXC opt-in"
)
SPEC = SandboxSpec(
    kind="native-conformance",
    work_dir=None,
    requires=frozenset(),
    program=ProgramRequirements(max_program_bytes=65536),
)


@pytest.fixture
def native_config(tmp_path):
    helper = Path(os.environ["MXC_ADAPTER_HELPER"])
    startup = Path(os.environ["MXC_ADAPTER_STARTUP"])
    return MxcSandboxConfig(
        helper,
        startup,
        tmp_path / "store",
        "mxc-1.0.1-directory-closed-v1",
        digest(helper),
        startup_identity(startup),
        Limits(32 * 1024**3, 64 * 1024**3, result_bytes=32 * 1024**2),
        ScratchLimits(6 * 1024**3, 100_000, 12 * 1024**3),
        persistent=True,
    )


class Subject:
    working_directory = "/workspace/calls/probe"

    def __init__(self, backend, key):
        self.backend, self.key = backend, key
        self.capabilities = backend.declarations.capabilities
        self.sandbox: Sandbox = cast(Sandbox, Access(self))

    async def call(self, code):
        async with self.backend.call_admission(self.key, SPEC, owner="native", timeout=90):
            sandbox = await self.backend.acquire(self.key, SPEC)
            return await sandbox.run_call(
                uuid.uuid4().hex, code, guest_call_path="probe", timeout=90
            )

    async def read_in_guest(self, path):
        result = await self.call(
            "import base64\nwith open(" + repr(path) + ", 'rb') as stream:\n"
            " print(base64.b64encode(stream.read()).decode('ascii'))"
        )
        assert result.exit_code == 0 and not result.stderr_bytes
        return base64.b64decode(result.stdout_bytes.strip(), validate=True)

    async def assert_links_refused(self):
        result = await self.call(
            "import os\nwith open('link-control', 'wb') as stream: stream.write(b'control')\n"
            "for operation in (os.symlink, os.link):\n"
            " try: operation('link-control', 'denied-link')\n"
            " except (OSError, NotImplementedError): pass\n"
            " else: raise AssertionError('guest created a link')\n"
            " assert not os.path.lexists('denied-link')\n"
            "os.unlink('link-control')\nprint('links-refused')"
        )
        assert result.stdout == "links-refused\n" and not result.stderr_bytes

    async def plant_file(self, path, content):
        await self.sandbox.write_file(path, content, working_directory=self.working_directory)

    async def exists(self, path):
        return (
            await self.sandbox.stat_file(path, working_directory=self.working_directory) is not None
        )

    async def dispose(self):
        assert await self.backend.dispose(self.key, kind=SPEC.kind) is None

    async def plant_symlink(self, path: str, target: str) -> None:
        raise NotImplementedError("native link creation must be refused")

    async def plant_directory_the_guest_owns(self, path: str) -> bool:
        raise NotImplementedError("native file conformance uses guest read controls")

    async def plant_file_the_guest_owns(self, path: str) -> bool:
        raise NotImplementedError("native file conformance uses guest read controls")

    async def the_guest_can_write(self, path: str) -> bool:
        raise NotImplementedError("native file conformance uses guest read controls")

    async def the_guest_can_delete(self, path: str) -> bool:
        raise NotImplementedError("native file conformance uses guest read controls")


class Access:
    """Enter the backend's real admission scope for each conformance operation."""

    def __init__(self, subject):
        self.subject = subject

    def __getattr__(self, method):
        async def access(*args, **kwargs):
            subject = self.subject
            async with subject.backend.call_admission(subject.key, SPEC, owner="probe", timeout=90):
                sandbox = await subject.backend.acquire(subject.key, SPEC)
                return await getattr(sandbox, method)(*args, **kwargs)

        return access


def test_native_file_contract(native_config):
    async def check():
        backend = MxcSandboxBackend(native_config)
        await backend.initialize()
        subject = Subject(backend, SandboxKey("native", "files", "agent"))
        try:
            await assert_files_in_conformance(subject)
            await assert_files_out_conformance(subject)
            await assert_files_delete_conformance(subject)
            await assert_reclaim_conformance(subject)
            await assert_exec_conformance(subject)
        finally:
            await backend.aclose()

    asyncio.run(check())


def test_native_call_scope(native_config):
    async def check():
        backend = MxcSandboxBackend(native_config)
        await backend.initialize()
        first = Subject(backend, SandboxKey("native", "scope", "agent", "first"))
        second = Subject(backend, SandboxKey("native", "scope", "agent", "second"))

        async def acquire_second():
            async with backend.call_admission(second.key, SPEC, owner="second", timeout=90):
                await backend.acquire(second.key, SPEC)
            return second

        try:
            await assert_call_scope_conformance(
                first,
                acquire_second,
                first.dispose,
                second.dispose,
            )
        finally:
            await backend.aclose()

    asyncio.run(check())


def test_native_restart_and_retry_without_execution(native_config):
    key = SandboxKey("native", "restart", "agent")
    code = "counter = globals().get('counter', 0) + 1\nprint(counter)"

    async def check():
        backend = MxcSandboxBackend(native_config)
        await backend.initialize()
        try:
            async with backend.call_admission(key, SPEC, owner="first", timeout=90):
                sandbox = await backend.acquire(key, SPEC)
                first = await sandbox.run_call("first", code, guest_call_path="one", timeout=90)
                assert first.stdout == "1\n"
                identity = sandbox.instance_id
        finally:
            await backend.aclose()
        backend = MxcSandboxBackend(native_config)
        try:
            async with backend.call_admission(key, SPEC, owner="recovered", timeout=90):
                sandbox = await backend.acquire(key, SPEC)
                assert sandbox.instance_id == identity
                replay = await sandbox.run_call("first", code, guest_call_path="one", timeout=90)
                assert replay == first
                second = await sandbox.run_call("second", code, guest_call_path="two", timeout=90)
                assert second.stdout == "2\n"
        finally:
            await backend.aclose()

    asyncio.run(check())


def test_native_fresh_state_and_cancellation_reclaim(native_config):
    async def check():
        backend = MxcSandboxBackend(replace(native_config, persistent=False))
        await backend.initialize()
        key = SandboxKey("native", "cancellation", "agent")
        try:
            async with backend.call_admission(key, SPEC, owner="native", timeout=90):
                sandbox = await backend.acquire(key, SPEC)
                code = "counter=globals().get('counter',0)+1\nprint(counter)"
                for call_id in ("first", "second"):
                    result = await sandbox.run_call(
                        call_id, code, guest_call_path=call_id, timeout=90
                    )
                    assert result.stdout == "1\n"
                task = asyncio.create_task(
                    sandbox.run_call(
                        "cancelled",
                        "while True: pass",
                        guest_call_path="cancelled",
                        timeout=90,
                    )
                )
                try:
                    async with asyncio.timeout(90):
                        while not list((native_config.root / "scratch").glob("*/native.ready")):
                            if task.done():
                                await task
                                raise AssertionError("unbounded guest unexpectedly finished")
                            await asyncio.sleep(0.02)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    if not task.done():
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                assert not list((native_config.root / "scratch").iterdir())
                result = await sandbox.run_call("after", code, guest_call_path="after", timeout=90)
                assert result.stdout == "1\n"
        finally:
            await backend.aclose()

    asyncio.run(check())
