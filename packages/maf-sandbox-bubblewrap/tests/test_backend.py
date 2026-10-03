"""Portable admission checks and opt-in tests against actual Linux namespaces and cgroups."""

import asyncio
import base64
import json
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from maf_sandbox import (
    DeclaredOutput,
    Egress,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
    collect_outputs,
    make_file_system_sink,
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
from maf_sandbox_bubblewrap._backend import FILE_LIMIT

live = pytest.mark.skipif(
    sys.platform != "linux"
    or not all(
        os.environ.get(name)
        for name in ("MAF_BWRAP_RUNTIME", "MAF_BWRAP_STATE", "MAF_BWRAP_CGROUP")
    ),
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
def test_live_startup_refuses_unusable_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import maf_sandbox_bubblewrap._backend as module

    blocked = tmp_path / "blocked-shell"
    blocked.write_bytes(b"")
    blocked.chmod(0o644)
    original = module._arguments

    def arguments(settings: BubblewrapSandboxConfig) -> list[str]:
        args = original(settings)
        index = args.index("--")
        args[index:index] = ["--ro-bind", str(blocked), "/bin/sh"]
        return args

    monkeypatch.setattr(module, "_arguments", arguments)

    async def run() -> None:
        settings = replace(config(), state_root=tmp_path / "state")
        before = set(settings.cgroup_root.glob("maf-*"))
        with pytest.raises(PermissionError):
            await BubblewrapSandboxBackend.create(settings)
        assert set(settings.cgroup_root.glob("maf-*")) == before
        assert not list(settings.state_root.glob("*.json"))

    asyncio.run(run())


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
def test_live_transfer_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        backend = await BubblewrapSandboxBackend.create(config())
        identity = key()
        spec = SandboxSpec(
            kind="transfer",
            work_dir="/maf-sandbox/work",
            declared_outputs=(DeclaredOutput(path="result"),),
            files_out=TransferLimits(max_bytes_per_file=1, max_total_bytes=1, max_files=1),
        )
        sandbox = await backend.acquire(identity, spec)
        try:
            await sandbox.write_file("result", b"a", working_directory=".")
            original_stat = sandbox.stat_file

            async def grow_after_stat(path: str, *, working_directory: str):
                entry = await original_stat(path, working_directory=working_directory)
                await sandbox.write_file(path, b"grown", working_directory=working_directory)
                return entry

            monkeypatch.setattr(sandbox, "stat_file", grow_after_stat)
            with pytest.raises(SandboxTransferCapExceeded):
                await collect_outputs(sandbox, spec, sink=make_file_system_sink(tmp_path))
            assert not list(tmp_path.iterdir())
            with pytest.raises(SandboxTransferCapExceeded):
                await sandbox._request(
                    "write",
                    path="result",
                    directory=".",
                    data=base64.b64encode(bytes(FILE_LIMIT + 1)).decode(),
                )
            assert await sandbox.read_file("result", working_directory=".", max_bytes=5) == b"grown"
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


@live
@pytest.mark.parametrize(
    "stage", ["record-write", "record-published", "group-created", "limits-written"]
)
@pytest.mark.parametrize("failure", ["exit", "raise"])
def test_live_startup_record_recovery(tmp_path: Path, stage: str, failure: str) -> None:
    async def run() -> None:
        settings = replace(config(), state_root=tmp_path / "state")
        backend = await BubblewrapSandboxBackend.create(settings)
        identity = key()
        before = set(settings.cgroup_root.glob("maf-*"))
        program = """
import asyncio, json, os, sys
from pathlib import Path
from maf_sandbox import SandboxKey, SandboxSpec
from maf_sandbox_bubblewrap import BubblewrapSandboxBackend, BubblewrapSandboxConfig
runtime, state, group, scope, stage, failure = sys.argv[1:]
backend = BubblewrapSandboxBackend(BubblewrapSandboxConfig(Path(runtime), Path(state), Path(group)))
def interrupt():
    if failure == 'exit':
        os._exit(73)
    raise RuntimeError('injected startup failure')
original_mkdir = Path.mkdir
def mkdir(path, *args, **kwargs):
    if path.parent == Path(group) and stage == 'record-published':
        interrupt()
    result = original_mkdir(path, *args, **kwargs)
    if path.parent == Path(group) and stage == 'group-created':
        interrupt()
    return result
Path.mkdir = mkdir
if stage == 'record-write':
    def dump(value, file, *args, **kwargs):
        file.write('{')
        file.flush()
        interrupt()
    json.dump = dump
if stage == 'limits-written':
    async def launch(*args, **kwargs):
        interrupt()
    asyncio.create_subprocess_exec = launch
async def run():
    try:
        await backend.acquire(SandboxKey(scope, 'thread', 'agent'), SandboxSpec(kind='startup'))
    except RuntimeError as error:
        assert str(error) == 'injected startup failure'
        sys.exit(73)
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
            stage,
            failure,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
            assert process.returncode == 73, (stdout, stderr)
            groups = set(settings.cgroup_root.glob("maf-*")) - before
            records = [backend._read_record(path) for path in settings.state_root.glob("*.json")]
            if failure == "raise" or stage == "record-write":
                assert not records and not groups
            else:
                assert len(records) == 1
                assert records[0]["identity"] == [identity.scope, "thread", "agent", "", "startup"]
                assert groups <= {backend._group(records[0]["instance"])}
            sandbox = await backend.acquire(identity, SandboxSpec(kind="startup"))
            assert (await sandbox.exec(["true"], working_directory=".", timeout=5)).exit_code == 0
            assert not list(settings.state_root.glob("*.pending"))
            assert await backend.dispose(identity) is None
            assert not list(settings.state_root.glob("*.json"))
            assert set(settings.cgroup_root.glob("maf-*")) == before
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await backend.dispose(identity)
            for group in set(settings.cgroup_root.glob("maf-*")) - before:
                await backend.kill_group(group.name.removeprefix("maf-"))

    asyncio.run(run())


@live
@pytest.mark.parametrize("cancel", [False, True])
def test_live_queued_exec_preserves_active_command(cancel: bool) -> None:
    async def run() -> None:
        backend = await BubblewrapSandboxBackend.create(config())
        identity = key()
        sandbox = await backend.acquire(identity, SandboxSpec(kind="queued"))
        active = asyncio.create_task(
            sandbox.exec(["sleep", "0.5"], working_directory=".", timeout=5)
        )
        queued = None
        try:
            async with asyncio.timeout(5):
                while not sandbox._serial.locked():
                    await asyncio.sleep(0)
            queued = asyncio.create_task(
                sandbox.exec(["touch", "queued-marker"], working_directory=".", timeout=0.02)
            )
            if cancel:
                await asyncio.sleep(0)
                queued.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await queued
            assert not active.done()
            assert (await active).exit_code == 0
            assert await sandbox.stat_file("queued-marker", working_directory=".") is None
            assert (await sandbox.exec(["true"], working_directory=".", timeout=5)).exit_code == 0
        finally:
            if queued is not None:
                queued.cancel()
                await asyncio.gather(queued, return_exceptions=True)
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            assert await backend.dispose(identity) is None

    asyncio.run(run())


@live
def test_live_exec_timeout_admission() -> None:
    async def run() -> None:
        backend = await BubblewrapSandboxBackend.create(replace(config(), max_timeout=0.5))
        identity = key()
        sandbox = await backend.acquire(identity, SandboxSpec(kind="timeout"))
        try:
            with pytest.raises(ValueError, match="timeout"):
                await sandbox.exec(["touch", "marker"], working_directory=".", timeout=1)
            assert await sandbox.stat_file("marker", working_directory=".") is None
            assert (await sandbox.exec(["true"], working_directory=".", timeout=0.5)).exit_code == 0
            with pytest.raises(TimeoutError):
                await sandbox.exec(["sleep", "5"], working_directory=".", timeout=0.1)
            assert not backend._group(sandbox.instance_id).exists()
        finally:
            assert await backend.dispose(identity) is None

    asyncio.run(run())
