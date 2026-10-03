"""Engine-free Linux namespaces, with host-owned lifecycle records and cgroup supervision."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from maf_sandbox import (
    BackendDeclarations,
    Capability,
    DisposalFailure,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    OsFamily,
    Sandbox,
    SandboxBackend,
    SandboxEntry,
    SandboxKey,
    SandboxSpec,
    SandboxTransferCapExceeded,
    ScopePurge,
    fold_disposal_failures,
)

from ._config import BubblewrapSandboxConfig

FILE_LIMIT = 8 * 1024 * 1024
FRAME_LIMIT = 12 * 1024 * 1024
_CAPABILITIES = frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT})


def _identity(key: SandboxKey, kind: str) -> list[str]:
    return [key.scope, key.thread_id, key.agent_id, key.call_id, kind]


def _digest(identity: list[str]) -> str:
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _lock(path: Path) -> int:
    import fcntl

    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_record(path: Path, value: dict[str, Any]) -> None:
    """Publish complete ownership under the caller's exclusive record lock."""
    pending = path.with_suffix(".pending")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


async def _finish(task: asyncio.Task[None]) -> None:
    """Drain cleanup even if the owning task receives repeated cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _diagnostics(stream: asyncio.StreamReader) -> str:
    retained = bytearray()
    while chunk := await stream.read(4096):
        retained.extend(chunk[: max(0, 8192 - len(retained))])
    return retained.decode("utf-8", errors="replace")


def _arguments(config: BubblewrapSandboxConfig) -> list[str]:
    args = [str(config.bwrap)]
    args.extend("--unshare-" + name for name in ("user", "pid", "net", "ipc", "uts", "cgroup"))
    args.extend(
        [
            "--disable-userns",
            "--assert-userns-disabled",
            "--as-pid-1",
            "--new-session",
            "--die-with-parent",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--hostname",
            "sandbox",
            "--ro-bind",
            str(config.runtime_root),
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
    )
    for directory in ("/tmp", "/run", "/maf-sandbox"):
        args.extend(["--size", str(config.workspace_bytes), "--tmpfs", directory])
    args.extend(
        [
            "--ro-bind",
            str(Path(__file__).with_name("_guest.py")),
            "/run/maf-broker.py",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "DRAWIO_DISABLE_UPDATE",
            "true",
            "--chdir",
            "/",
            "--",
            "/usr/bin/python3",
            "-I",
            "/run/maf-broker.py",
        ]
    )
    return args


class _Sandbox:
    """One supervised namespace; command output and transfer frames are independently bounded."""

    def __init__(
        self,
        backend: BubblewrapSandboxBackend,
        instance: str,
        process: asyncio.subprocess.Process,
        lock_fd: int,
        record: Path,
        spec: SandboxSpec,
        diagnostics: asyncio.Task[str],
    ) -> None:
        self.backend = backend
        self.instance_id = instance
        self.process = process
        self.lock_fd = lock_fd
        self.record = record
        self.spec = spec
        self.diagnostics = diagnostics
        self.dead = False
        self._serial = asyncio.Lock()
        self._sequence = 0

    async def _request(
        self,
        op: str,
        *,
        transport_timeout: float = 10,
        deadline: float | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + transport_timeout
        async with asyncio.timeout_at(deadline):
            await self._serial.acquire()
        try:
            if self.dead or self.process.returncode is not None:
                raise RuntimeError("Sandbox is no longer running")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Request deadline expired before dispatch")
            if op == "exec":
                fields["timeout"] = min(fields["timeout"], remaining)
            self._sequence += 1
            payload = json.dumps({"id": self._sequence, "op": op, **fields}).encode() + b"\n"
            if len(payload) > FRAME_LIMIT:
                raise ValueError("Request exceeds transfer frame limit")
            assert self.process.stdin is not None and self.process.stdout is not None
            if loop.time() >= deadline:
                raise TimeoutError("Request deadline expired before dispatch")
            try:
                async with asyncio.timeout_at(deadline):
                    self.process.stdin.write(payload)
                    await self.process.stdin.drain()
                    frame = await self.process.stdout.readline()
                    if not frame.endswith(b"\n") or len(frame) > FRAME_LIMIT:
                        raise RuntimeError("Invalid guest response frame")
                    response: dict[str, Any] = json.loads(frame)
                    if type(response) is not dict or response.get("id") != self._sequence:
                        raise RuntimeError("Invalid guest response identity")
            except BaseException:
                await _finish(asyncio.create_task(self.stop()))
                raise
            if "error" in response:
                error = response["error"]
                detail = str(response.get("detail", "Guest operation refused"))[:1024]
                if error == "TransferCapExceeded":
                    raise SandboxTransferCapExceeded(detail)
                if error == "FileNotFoundError":
                    raise FileNotFoundError(detail)
                if error == "TimeoutError":
                    await _finish(asyncio.create_task(self.stop()))
                    raise TimeoutError(detail)
                if error in {
                    "OSError",
                    "IsADirectoryError",
                    "NotADirectoryError",
                    "PermissionError",
                }:
                    errors: dict[str, type[OSError]] = {
                        "OSError": OSError,
                        "IsADirectoryError": IsADirectoryError,
                        "NotADirectoryError": NotADirectoryError,
                        "PermissionError": PermissionError,
                    }
                    raise errors[error](detail)
                raise ValueError(detail)
            result = response.get("result")
            if not isinstance(result, dict):
                await _finish(asyncio.create_task(self.stop()))
                raise RuntimeError("Invalid guest result")
            return cast(dict[str, Any], result)
        finally:
            self._serial.release()

    async def stop(self) -> None:
        """Terminate the whole resource group before releasing ownership."""
        self.dead = True
        await self.backend.kill_group(self.instance_id)
        await asyncio.wait_for(self.process.wait(), 10)
        await self.diagnostics
        if self.process.stdin is not None:
            self.process.stdin.close()

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        """Write a regular file through no-follow guest descriptors."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        if len(data) > FILE_LIMIT:
            raise SandboxTransferCapExceeded("File exceeds transfer limit")
        await self._request(
            "write", path=path, directory=working_directory, data=base64.b64encode(data).decode()
        )

    async def exec(
        self,
        command: str | Sequence[str],
        *,
        working_directory: str,
        timeout: float,
    ) -> ExecResult:
        """Execute within the configured deadline and cap combined stdout/stderr bytes."""
        if type(timeout) not in (int, float) or not 0 < timeout < math.inf:
            raise ValueError("timeout must be finite and positive")
        if timeout > self.backend.config.max_timeout:
            raise ValueError("timeout exceeds configured max_timeout")
        deadline = asyncio.get_running_loop().time() + timeout
        result = await self._request(
            "exec",
            command=command if isinstance(command, str) else list(command),
            directory=working_directory,
            timeout=timeout,
            transport_timeout=timeout,
            deadline=deadline,
            output_limit=self.backend.config.output_bytes,
        )
        stdout = base64.b64decode(result["stdout"], validate=True)
        stderr = base64.b64decode(result["stderr"], validate=True)
        if (
            len(stdout) + len(stderr) > self.backend.config.output_bytes
            or type(result["exit_code"]) is not int
        ):
            raise RuntimeError("Invalid guest execution result")
        return ExecResult(stdout_bytes=stdout, stderr_bytes=stderr, exit_code=result["exit_code"])

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        """Report a guest entry; the isolated broker is not a host filesystem attestation."""
        try:
            result = await self._request("stat", path=path, directory=working_directory)
        except FileNotFoundError:
            return None
        if result.get("missing") is True:
            return None
        size = result["size"]
        if size is not None and (type(size) is not int or size < 0):
            raise RuntimeError("Invalid guest file size")
        return SandboxEntry(path, EntryKind(result["kind"]), size)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        """Read a bounded regular file without following links."""
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a nonnegative integer")
        limit = min(max_bytes, FILE_LIMIT)
        result = await self._request(
            "read", path=path, directory=working_directory, max_bytes=limit
        )
        data = base64.b64decode(result["data"], validate=True)
        if len(data) > limit:
            raise SandboxTransferCapExceeded("File exceeds transfer limit")
        return data

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        """RUN_CODE is not supported; use EXEC with an installed interpreter."""
        raise NotImplementedError

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """FILES_DELETE is not declared."""
        raise NotImplementedError

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Use disposal; no weaker cleanup capability is declared."""
        raise NotImplementedError

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        """FILES_LIST is not declared."""
        raise NotImplementedError

    async def reset(self, *, timeout: float) -> None:
        """SNAPSHOT is not declared."""
        raise NotImplementedError


class BubblewrapSandboxBackend:
    """A Linux backend requiring working namespaces and delegated cgroup v2 controllers.

    Construct through create() to probe the actual boundary. Guest file metadata is broker
    reported; no host file plane exists. Call admission is exclusive within one router, and
    a host flock refuses simultaneous owners across backend objects or processes.
    """

    name = "bubblewrap"
    isolation = Isolation.CONTAINER
    declarations = BackendDeclarations(
        capabilities=_CAPABILITIES,
        egress_modes=frozenset({Egress.CLOSED}),
        os_families=frozenset({OsFamily.POSIX}),
        isolation_scopes=frozenset({IsolationScope.CALL, IsolationScope.CONVERSATION}),
        requires_exclusive_admission=True,
    )

    def __init__(self, config: BubblewrapSandboxConfig) -> None:
        if sys.platform != "linux":
            raise RuntimeError("Bubblewrap requires Linux; no subprocess fallback is available")
        self.config = config
        self._sandboxes: dict[str, _Sandbox] = {}
        self._serial = asyncio.Lock()

    @classmethod
    async def create(cls, config: BubblewrapSandboxConfig) -> BubblewrapSandboxBackend:
        """Refuse missing runtime, resource controllers or namespace prerequisites."""
        backend = cls(config)
        for path in (config.runtime_root, config.cgroup_root, config.bwrap):
            if path.is_symlink() or not path.exists():
                raise ValueError(f"Missing or symlinked prerequisite: {path}")
        config.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = config.state_root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("state_root must be a private, owned directory")
        if config.output_bytes > FILE_LIMIT:
            raise ValueError("output_bytes must not exceed 8 MiB")
        key = SandboxKey("probe-" + uuid4().hex, "probe", "probe")
        sandbox = await backend.acquire(key, SandboxSpec(kind="probe", requires=_CAPABILITIES))
        try:
            result = await sandbox.exec(
                ["/bin/true"], working_directory=".", timeout=min(10, config.max_timeout)
            )
            if result.exit_code != 0:
                raise RuntimeError("Namespace probe failed")
        finally:
            failure = await backend.dispose(key)
            if failure is not None:
                raise RuntimeError(str(failure))
        return backend

    def _group(self, instance: str) -> Path:
        if len(instance) != 32 or any(char not in "0123456789abcdef" for char in instance):
            raise ValueError("Invalid sandbox instance identity")
        return self.config.cgroup_root / ("maf-" + instance)

    async def kill_group(self, instance: str) -> None:
        """Kill an owned resource group and wait for its last process to leave."""
        group = self._group(instance)
        if not group.exists():
            return
        if not (group / "cgroup.kill").exists():
            group.rmdir()
            return
        fd = os.open(group / "cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)
        async with asyncio.timeout(10):
            while "populated 1" in (group / "cgroup.events").read_text("ascii"):
                await asyncio.sleep(0.02)
        group.rmdir()

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> _Sandbox:
        """Get or create an exact key/kind; refuse conflicting specifications or owners."""
        if (
            spec.egress != Egress.CLOSED
            or not spec.required_capabilities <= _CAPABILITIES
            or spec.work_dir not in (None, "/maf-sandbox/work")
            or spec.image not in (None, self.config.runtime_id)
            or spec.image_id is not None
        ):
            raise ValueError("Unsupported Bubblewrap specification")
        identity = _identity(key, spec.kind)
        if len(json.dumps(identity).encode()) > 8192:
            raise ValueError("Sandbox identity exceeds record limit")
        digest = _digest(identity)
        async with self._serial:
            existing = self._sandboxes.get(digest)
            if existing is not None:
                if existing.dead or existing.spec != spec:
                    raise RuntimeError("Dispose the prior sandbox before replacing it")
                return existing
            record = self.config.state_root / (digest + ".json")
            lock_fd = _lock(record.with_suffix(".lock"))
            instance = uuid4().hex
            group = self._group(instance)
            process: asyncio.subprocess.Process | None = None
            diagnostics: asyncio.Task[str] | None = None
            try:
                if record.exists():
                    previous = self._read_record(record)
                    if previous["identity"] != identity:
                        raise ValueError("Sandbox record identity mismatch")
                    await self.kill_group(previous["instance"])
                    record.unlink()
                _write_record(
                    record,
                    {
                        "identity": identity,
                        "instance": instance,
                        "cgroup_root": str(self.config.cgroup_root),
                    },
                )
                group.mkdir(mode=0o700)
                for name, value in (
                    ("memory.max", str(self.config.memory_bytes)),
                    ("memory.swap.max", "0"),
                    ("memory.oom.group", "1"),
                    ("pids.max", str(self.config.pids)),
                    ("cpu.max", f"{self.config.cpu_quota} 100000"),
                ):
                    with (group / name).open("r+", encoding="ascii") as limit_file:
                        limit_file.write(value)
                if not (group / "cgroup.kill").exists():
                    raise RuntimeError("cgroup v2 kill support is required")
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("_launch.py")),
                    str(os.getpid()),
                    str(group),
                    *_arguments(self.config),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=FRAME_LIMIT,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                )
                assert process.stdout is not None
                assert process.stderr is not None
                diagnostics = asyncio.create_task(_diagnostics(process.stderr))
                async with asyncio.timeout(10):
                    if await process.stdout.readline() != b'{"ready":1}\n':
                        detail = await diagnostics
                        raise RuntimeError("Bubblewrap startup failed: " + detail)
                sandbox = _Sandbox(self, instance, process, lock_fd, record, spec, diagnostics)
                self._sandboxes[digest] = sandbox
                return sandbox
            except BaseException:

                async def cleanup() -> None:
                    await self.kill_group(instance)
                    if process is not None:
                        await asyncio.wait_for(process.wait(), 10)
                    if diagnostics is not None:
                        await diagnostics
                    if record.exists():
                        owned = self._read_record(record)
                        if owned["instance"] == instance:
                            record.unlink()

                try:
                    await _finish(asyncio.create_task(cleanup()))
                finally:
                    os.close(lock_fd)
                raise

    def _read_record(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError("Invalid sandbox record")
        value: dict[str, Any] = json.loads(path.read_text("utf-8"))
        if type(value["identity"]) is not list:
            raise ValueError("Invalid sandbox record identity")
        identity = cast(list[object], value["identity"])
        if (
            len(identity) != 5
            or not all(isinstance(item, str) for item in identity)
            or _digest(cast(list[str], identity)) != path.stem
            or value["cgroup_root"] != str(self.config.cgroup_root)
        ):
            raise ValueError("Sandbox record ownership mismatch")
        self._group(value["instance"])
        return value

    async def _dispose_record(self, record: Path, instance_id: str | None) -> bool:
        local = self._sandboxes.get(record.stem)
        lock_fd = local.lock_fd if local is not None else _lock(record.with_suffix(".lock"))
        release = local is None
        try:
            if not record.exists():
                return False
            value = self._read_record(record)
            if instance_id is not None and value["instance"] != instance_id:
                return False
            if local is not None:
                await local.stop()
                self._sandboxes.pop(record.stem)
                release = True
            else:
                await self.kill_group(value["instance"])
            record.unlink()
            return True
        finally:
            if release:
                os.close(lock_fd)

    async def dispose(
        self,
        key: SandboxKey,
        *,
        kind: str | None = None,
        instance_id: str | None = None,
    ) -> DisposalFailure | None:
        """Remove only matching owned records; a stale instance never targets its replacement."""
        failures: list[DisposalFailure] = []
        async with self._serial:
            try:
                for record in self.config.state_root.glob("*.json"):
                    try:
                        value = self._read_record(record)
                        if value["identity"][:4] == _identity(key, "")[:4] and (
                            kind is None or value["identity"][4] == kind
                        ):
                            await self._dispose_record(record, instance_id)
                    except Exception as error:
                        failures.append(DisposalFailure("refused", str(error)))
            except Exception as error:
                failures.append(DisposalFailure("refused", str(error)))
        return fold_disposal_failures(failures)

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        """Sweep persisted records, retaining failures and refusing another live owner's lock."""
        failures: list[DisposalFailure] = []
        disposed = 0
        async with self._serial:
            for record in self.config.state_root.glob("*.json"):
                try:
                    value = self._read_record(record)
                    if value["identity"][:2] == [scope, thread_id]:
                        disposed += await self._dispose_record(record, None)
                except Exception as error:
                    failures.append(DisposalFailure("refused", str(error)))
        return ScopePurge(disposed, fold_disposal_failures(failures))


if TYPE_CHECKING:
    _: tuple[SandboxBackend, type[Sandbox]] = (
        BubblewrapSandboxBackend(
            BubblewrapSandboxConfig(Path("/runtime"), Path("/state"), Path("/cgroup"))
        ),
        _Sandbox,
    )
