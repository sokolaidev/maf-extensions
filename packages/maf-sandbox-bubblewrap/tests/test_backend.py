"""Portable admission checks and opt-in tests against actual Linux namespaces and cgroups."""

import asyncio
import json
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from maf_sandbox import (
    Egress,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.conformance import (
    PosixGuestSubject,
    assert_call_scope_conformance,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
    assert_reclaim_conformance,
    assert_storage_base_conformance,
)

from maf_sandbox_bubblewrap import BubblewrapSandboxBackend, BubblewrapSandboxConfig

live = pytest.mark.skipif(
    sys.platform != "linux" or "MAF_BWRAP_RUNTIME" not in os.environ,
    reason="requires an explicitly provisioned Linux runtime and delegated cgroup v2 subtree",
)


def config() -> BubblewrapSandboxConfig:
    return BubblewrapSandboxConfig(
        runtime_root=Path(os.environ["MAF_BWRAP_RUNTIME"]),
        state_root=Path(os.environ["MAF_BWRAP_STATE"]),
        cgroup_root=Path(os.environ["MAF_BWRAP_CGROUP"]),
    )


def key(call: str = "") -> SandboxKey:
    return SandboxKey("test-" + uuid4().hex, "thread", "agent", call_id=call)


def test_configuration_refuses_invalid_limits(tmp_path: Path) -> None:
    settings = BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path)
    for field in ("memory_bytes", "pids", "cpu_quota", "workspace_bytes", "output_bytes"):
        for invalid in (0, -1, True, 1.5):
            with pytest.raises(ValueError):
                replace(settings, **{field: invalid})
    for timeout in (0, -1, float("inf"), float("nan"), 3601):
        with pytest.raises(ValueError):
            replace(settings, max_timeout=timeout)


def test_no_other_platform_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="requires Linux"):
        BubblewrapSandboxBackend(BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path))


@live
@pytest.mark.parametrize("suite", ["storage", "in", "out", "exec", "delete", "reclaim"])
def test_live_protocol_conformance(suite: str) -> None:
    async def run() -> None:
        backend = await BubblewrapSandboxBackend.create(config())
        identity = key()
        sandbox = await backend.acquire(identity, SandboxSpec(kind="conformance"))
        try:
            capabilities = backend.declarations.capabilities
            subject = PosixGuestSubject(sandbox, "/maf-sandbox/work", capabilities)
            if suite == "storage":
                await assert_storage_base_conformance(sandbox, capabilities)
            elif suite == "in":
                await assert_files_in_conformance(subject)
            elif suite == "out":
                await assert_files_out_conformance(subject)
            elif suite == "delete":
                with pytest.raises(ValueError):
                    await assert_files_delete_conformance(subject)
            elif suite == "reclaim":
                with pytest.raises(ValueError):
                    await assert_reclaim_conformance(subject)
            else:
                await assert_exec_conformance(subject)
        finally:
            assert await backend.dispose(identity) is None

    asyncio.run(run())


@live
def test_live_ownership_isolation_and_stale_disposal() -> None:
    async def run() -> None:
        backend = await BubblewrapSandboxBackend.create(config())
        other = await BubblewrapSandboxBackend.create(config())
        identity = key("first")
        second = SandboxKey(identity.scope, identity.thread_id, identity.agent_id, call_id="second")
        spec = SandboxSpec(kind="ownership")
        first = await backend.acquire(identity, spec)
        sibling = await backend.acquire(second, spec)
        try:
            assert await backend.acquire(identity, spec) is first
            with pytest.raises(OSError):
                await other.acquire(identity, spec)
            busy = await other.dispose_scope(identity.scope, identity.thread_id)
            assert busy.undisposed and busy.disposed == 0
            await first.write_file("marker", b"private", working_directory=".")
            assert await sibling.stat_file("marker", working_directory=".") is None
            assert await backend.dispose(identity, instance_id=first.instance_id) is None
            replacement = await backend.acquire(identity, spec)
            assert replacement.instance_id != first.instance_id
            assert await backend.dispose(identity, instance_id=first.instance_id) is None
            assert (
                await replacement.exec(["true"], working_directory=".", timeout=5)
            ).exit_code == 0
            assert await replacement.stat_file("marker", working_directory=".") is None
        finally:
            purge = await backend.dispose_scope(identity.scope, identity.thread_id)
            assert purge.undisposed is None and purge.disposed == 2

    asyncio.run(run())


@live
def test_live_call_scope_conformance() -> None:
    async def run() -> None:
        backend = await BubblewrapSandboxBackend.create(config())
        first_key = key("first")
        other_key = SandboxKey(
            first_key.scope, first_key.thread_id, first_key.agent_id, call_id="other"
        )
        spec = SandboxSpec(kind="scope")
        sandbox = await backend.acquire(first_key, spec)
        subject = PosixGuestSubject(sandbox, "/maf-sandbox/work", backend.declarations.capabilities)

        async def acquire_other() -> PosixGuestSubject:
            other = await backend.acquire(other_key, spec)
            return PosixGuestSubject(other, "/maf-sandbox/work", backend.declarations.capabilities)

        async def dispose_first() -> None:
            assert await backend.dispose(first_key) is None

        async def dispose_other() -> None:
            assert await backend.dispose(other_key) is None

        try:
            await assert_call_scope_conformance(
                subject, acquire_other, dispose_first, dispose_other
            )
        finally:
            assert (
                await backend.dispose_scope(first_key.scope, first_key.thread_id)
            ).undisposed is None

    asyncio.run(run())


@live
def test_live_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "host-canary"
    secret.write_text("host-only")
    monkeypatch.setenv("MAF_HOST_SECRET", "must-not-enter-guest")

    async def run() -> None:
        backend = await BubblewrapSandboxBackend.create(config())
        identity = key()
        sandbox = await backend.acquire(identity, SandboxSpec(kind="boundary"))
        try:
            with pytest.raises(Exception):
                SandboxRouter([backend])
            with pytest.raises(ValueError):
                await backend.acquire(key(), SandboxSpec(kind="open", egress=Egress.UNRESTRICTED))
            with socket.socket() as server:
                server.bind(("127.0.0.1", 0))
                server.listen()
                program = f"""
import os, socket
assert not os.path.exists({str(secret)!r})
assert 'MAF_HOST_SECRET' not in os.environ
assert not os.path.exists('/sys/fs/cgroup/cgroup.procs')
assert not os.path.exists('/var/run/docker.sock')
for destination in [('127.0.0.1', {server.getsockname()[1]}), ('1.1.1.1', 443)]:
    sock = socket.socket()
    sock.settimeout(0.2)
    try:
        sock.connect(destination)
    except OSError:
        pass
    else:
        raise AssertionError('network reached host or internet')
try:
    open('/etc/guest-write-test', 'w').close()
except OSError:
    pass
else:
    raise AssertionError('runtime is writable')
try:
    open('/proc/1/fd/1', 'w').close()
except OSError:
    pass
else:
    raise AssertionError('broker control pipe is accessible')
"""
                result = await sandbox.exec(
                    ["python3", "-c", program], working_directory=".", timeout=5
                )
                assert result.exit_code == 0, result.stderr
            result = await sandbox.exec(
                ["unshare", "-Ur", "true"], working_directory=".", timeout=5
            )
            assert result.exit_code != 0
        finally:
            assert await backend.dispose(identity) is None
        assert secret.read_text() == "host-only"

    asyncio.run(run())


@live
def test_live_descendants_cancellation_and_output_limits() -> None:
    async def run() -> None:
        backend = await BubblewrapSandboxBackend.create(config())
        identity = key()
        spec = SandboxSpec(kind="processes")
        sandbox = await backend.acquire(identity, spec)
        try:
            detached = "import subprocess; subprocess.Popen(['sleep','60'], start_new_session=True)"
            result = await sandbox.exec(
                ["python3", "-c", detached], working_directory=".", timeout=5
            )
            assert result.exit_code == 0
            group = config().cgroup_root / ("maf-" + sandbox.instance_id)
            assert len((group / "cgroup.procs").read_text().split()) <= 2
            with pytest.raises(ValueError, match="output limit"):
                await sandbox.exec(
                    ["python3", "-c", "print('x'*2000000)"], working_directory=".", timeout=5
                )
            task = asyncio.create_task(
                sandbox.exec(["sleep", "60"], working_directory=".", timeout=90)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not group.exists()
            with pytest.raises(RuntimeError, match="no longer running"):
                await sandbox.exec(["true"], working_directory=".", timeout=5)
        finally:
            assert await backend.dispose(identity) is None

    asyncio.run(run())


@live
def test_live_resource_limits() -> None:
    async def run() -> None:
        settings = replace(
            config(), memory_bytes=96 * 1024 * 1024, pids=20, workspace_bytes=1024 * 1024
        )
        backend = await BubblewrapSandboxBackend.create(settings)
        identity = key()
        sandbox = await backend.acquire(identity, SandboxSpec(kind="limits"))
        group = settings.cgroup_root / ("maf-" + sandbox.instance_id)
        try:
            assert (group / "memory.max").read_text().strip() == str(settings.memory_bytes)
            assert (group / "memory.swap.max").read_text().strip() == "0"
            assert (group / "pids.max").read_text().strip() == "20"
            assert (group / "cpu.max").read_text().strip() == "200000 100000"
            with pytest.raises(OSError):
                await sandbox.write_file("full", bytes(2 * 1024 * 1024), working_directory=".")
            result = await sandbox.exec(
                [
                    "python3",
                    "-c",
                    """
import subprocess
try:
    for _ in range(40):
        subprocess.Popen(['sleep', '60'])
except BlockingIOError:
    print('pids-limited')
""",
                ],
                working_directory=".",
                timeout=10,
            )
            assert result.stdout.strip() == "pids-limited"
            with pytest.raises(RuntimeError):
                await sandbox.exec(
                    ["python3", "-c", "x = bytearray(256*1024*1024)"],
                    working_directory=".",
                    timeout=10,
                )
            assert not group.exists()
        finally:
            assert await backend.dispose(identity) is None

    asyncio.run(run())


@live
def test_live_owner_death_and_recovery() -> None:
    async def run() -> None:
        settings = config()
        identity = key()
        program = """
import asyncio, os, sys
from pathlib import Path
from maf_sandbox import SandboxKey, SandboxSpec
from maf_sandbox_bubblewrap import BubblewrapSandboxBackend, BubblewrapSandboxConfig
async def run():
    runtime, state, group, scope = sys.argv[1:]
    backend = await BubblewrapSandboxBackend.create(BubblewrapSandboxConfig(Path(runtime), Path(state), Path(group)))
    sandbox = await backend.acquire(SandboxKey(scope, 'thread', 'agent'), SandboxSpec(kind='death'))
    asyncio.create_task(sandbox.exec(['sh', '-c', 'setsid sleep 60 & wait'], working_directory='.', timeout=90))
    await asyncio.sleep(0.1)
    print(sandbox.instance_id, flush=True)
    os._exit(0)
asyncio.run(run())
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            str(settings.runtime_root),
            str(settings.state_root),
            str(settings.cgroup_root),
            identity.scope,
            stdout=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        instance = (await process.stdout.readline()).decode().strip()
        assert len(instance) == 32 and await process.wait() == 0
        group = settings.cgroup_root / ("maf-" + instance)
        async with asyncio.timeout(10):
            while "populated 1" in (group / "cgroup.events").read_text():
                await asyncio.sleep(0.02)
        restarted = await BubblewrapSandboxBackend.create(settings)
        purge = await restarted.dispose_scope(identity.scope, identity.thread_id)
        assert purge.disposed == 1 and purge.undisposed is None
        assert not group.exists()
        records = [json.loads(path.read_text()) for path in settings.state_root.glob("*.json")]
        assert not any(record["identity"][0] == identity.scope for record in records)

    asyncio.run(run())


@live
def test_live_missing_prerequisites_refuse(tmp_path: Path) -> None:
    async def run() -> None:
        for settings in (
            replace(config(), bwrap=tmp_path / "missing-bwrap"),
            replace(config(), runtime_root=tmp_path / "missing-runtime"),
            replace(config(), cgroup_root=tmp_path),
        ):
            with pytest.raises((OSError, ValueError, RuntimeError)):
                await BubblewrapSandboxBackend.create(settings)
        assert not list(config().state_root.glob("*.json"))

    asyncio.run(run())
