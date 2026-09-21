"""Packaged Python in a micro-VM, with one killable process per logical sandbox."""

from __future__ import annotations

import asyncio
import math
import platform
import sys
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from concurrent.futures import Future
from contextlib import AbstractContextManager, asynccontextmanager, suppress
from typing import TYPE_CHECKING, ClassVar, cast

from maf_sandbox import (
    DEFAULT_TRANSFER_LIMITS,
    BackendDeclarations,
    Capability,
    DisposalFailure,
    Egress,
    EgressRule,
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxCapabilityNotSupported,
    SandboxEntry,
    SandboxKey,
    SandboxQueuedTimeout,
    SandboxSpec,
    ScopePurge,
    fold_disposal_failures,
)

from ._admission import admit, require_owner
from ._config import HyperlightSandboxConfig
from ._files import GUEST_ROOT, OutputDirectory
from ._process import Worker
from ._wire import HyperlightWorkerError

BACKEND_NAME = "hyperlight"
RUNTIME_INSTRUCTIONS = (
    "Python statements execute in a persistent CPython 3.14 WebAssembly runtime. "
    "Print results; a final expression is not echoed. The host may reset state between calls. "
    "There is no shell, package installation, host filesystem or file-transfer channel. "
    "Host tool registration is unavailable. "
    "json, math and re are available. datetime, statistics, pickle and __future__ are absent; "
    "do not use future imports or assume the full desktop standard library. "
    "http_get(url) and http_post(url, body=..., content_type=...) provide HTTP where the host "
    "allowlist permits it; raw sockets are unavailable."
)
FILE_RUNTIME_INSTRUCTIONS = RUNTIME_INSTRUCTIONS.replace(
    "or file-transfer channel.",
    "or input file-transfer channel. Write output files directly under /output using flat "
    "filenames; nested directories and link creation are unavailable. Outputs are collected "
    "before cleanup and do not persist to another call.",
)


def check_host() -> None:
    """Require a supported host and exclusive ownership before using the registry."""
    if sys.platform not in {"win32", "linux"} or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        raise HyperlightWorkerError("Hyperlight requires x86-64 Windows WHP or Linux KVM")
    if sys.platform == "linux":
        from ._linux import claim_host
    else:
        from ._windows import claim_host

    claim_host()


def _deadline(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(cast("object", timeout), (int, float)):
        raise ValueError("timeout must be a positive finite number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    return time.monotonic() + timeout


@asynccontextmanager
async def _claim(lock: threading.Lock, deadline: float) -> AsyncGenerator[None]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxQueuedTimeout("deadline expired before the operation started")
        if lock.acquire(blocking=False):
            break
        await asyncio.sleep(min(0.01, remaining))
    try:
        yield
    finally:
        lock.release()


async def _offload[T](operation: Callable[[], T]) -> T:
    # Cleanup must never queue behind blocked readers in an executor pool.
    result: Future[T] = Future()

    def run() -> None:
        if not result.set_running_or_notify_cancel():
            return
        try:
            result.set_result(operation())
        except BaseException as error:
            result.set_exception(error)

    threading.Thread(target=run, daemon=True).start()
    return await asyncio.wrap_future(result)


async def _finish[T](task: asyncio.Task[T]) -> T:
    """Finish bounded cleanup, then propagate any cancellation received while waiting."""
    interrupted: asyncio.CancelledError | None = None
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if interrupted is None:
                    interrupted = error
        return task.result()
    finally:
        if interrupted is not None:
            raise interrupted


class _HyperlightSandbox:
    """A serial execution stream; a failed worker must be reacquired, never silently reused."""

    def __init__(
        self,
        config: HyperlightSandboxConfig,
        targets: tuple[str, ...],
        contract: str | None,
        owner: str,
        key: SandboxKey,
        kind: str,
    ) -> None:
        self.config = config
        self.targets = targets
        self.contract = contract
        self.owner = owner
        self.key = key
        self.kind = kind
        self._files_gate = threading.Lock()
        self._gate = threading.Lock()
        self._state = threading.Lock()
        self._identity = uuid.uuid4().hex
        self._retired = False
        self.outputs = OutputDirectory() if config.file_outputs else None
        try:
            self.worker = Worker(config)
        except BaseException:
            if self.outputs is not None:
                self.outputs.close()
            raise

    def _authorize(self) -> None:
        if self.outputs is not None:
            require_owner(self.key, self.kind)

    @property
    def instance_id(self) -> str:
        with self._state:
            return self._identity

    @property
    def alive(self) -> bool:
        with self._state:
            return not self._retired and self.worker.alive

    def retire(self, expected_id: str | None = None) -> bool:
        with self._state:
            if expected_id is not None and expected_id != self._identity:
                return False
            self._retired = True
            return True

    async def stop(self) -> None:
        self.retire()

        def close() -> None:
            self.worker.close()
            with self._files_gate:
                if self.outputs is not None:
                    self.outputs.close()

        await _finish(asyncio.create_task(_offload(close)))

    async def _exchange(self, message: dict[str, object], deadline: float) -> dict[str, object]:
        if not self.alive:
            raise HyperlightWorkerError("sandbox is retired; acquire a new sandbox")
        if time.monotonic() >= deadline:
            raise SandboxQueuedTimeout("deadline expired before the operation started")
        admission = threading.Lock()
        started = False
        withdrawn = False

        def exchange() -> dict[str, object]:
            nonlocal started
            with admission:
                if withdrawn or time.monotonic() >= deadline:
                    raise SandboxQueuedTimeout("deadline expired before the operation started")
                started = True
            return self.worker.request(message, deadline=deadline)

        task = asyncio.create_task(_offload(exchange))
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                response = await asyncio.shield(task)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Hyperlight operation exceeded its deadline")
                return response
        except BaseException as error:
            with admission:
                withdrawn = True
                dispatched = started
            try:
                if dispatched:
                    await self.stop()
            finally:
                if not dispatched or not self.worker.alive:
                    with suppress(Exception):
                        await _finish(task)
            if not dispatched and isinstance(error, TimeoutError):
                raise SandboxQueuedTimeout(
                    "deadline expired before the operation started"
                ) from error
            raise

    async def prepare(self, deadline: float) -> None:
        message: dict[str, object] = {
            "op": "init",
            "targets": self.targets,
            "output_limit": self.config.max_output_bytes,
        }
        if self.outputs is not None:
            message["output_dir"] = str(self.outputs.path)
        response = await self._exchange(
            message,
            deadline,
        )
        if response != {"ok": True}:
            raise HyperlightWorkerError("worker did not confirm preparation")

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        self._authorize()
        deadline = _deadline(timeout)
        if not isinstance(cast("object", code), str):
            raise TypeError("code must be Python source text")
        if len(code.encode("utf-8")) > self.config.max_code_bytes:
            raise ValueError("code exceeds max_code_bytes")
        async with _claim(self._gate, deadline):
            self._authorize()
            if self.outputs is not None:
                try:
                    self.outputs.validate()
                except BaseException:
                    await self.stop()
                    raise
            response = await self._exchange({"op": "run", "code": code}, deadline)
            if self.outputs is not None:
                try:
                    self.outputs.validate()
                except BaseException:
                    await self.stop()
                    raise
            stdout, stderr, status = (
                response.get("stdout"),
                response.get("stderr"),
                response.get("exit_code"),
            )
            if (
                not isinstance(stdout, str)
                or not isinstance(stderr, str)
                or type(status) is not int
            ):
                await self.stop()
                raise HyperlightWorkerError("invalid execution result")
            if len(stdout.encode()) + len(stderr.encode()) > self.config.max_output_bytes:
                await self.stop()
                raise HyperlightWorkerError("worker violated the output limit")
            if status < 0:
                await self.stop()
                raise HyperlightWorkerError("native execution failed")
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=status)

    async def reset(self, *, timeout: float) -> None:
        self._authorize()
        deadline = _deadline(timeout)
        async with _claim(self._gate, deadline):
            self._authorize()
            if self.outputs is not None:
                try:
                    self.outputs.validate()
                except BaseException:
                    await self.stop()
                    raise
            response = await self._exchange({"op": "reset"}, deadline)
            if response != {"ok": True}:
                await self.stop()
                raise HyperlightWorkerError("worker did not confirm restore")
            if self.outputs is not None:
                try:
                    self.outputs.clear()
                except BaseException:
                    await self.stop()
                    raise
            with self._state:
                if self._retired:
                    raise HyperlightWorkerError("sandbox was disposed during restore")
                self._identity = uuid.uuid4().hex

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        raise NotImplementedError("Hyperlight supplies RUN_CODE, not EXEC")

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        raise NotImplementedError("Hyperlight file channels are not enabled")

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        if self.outputs is None:
            raise NotImplementedError("Hyperlight file channels are not enabled")
        self._authorize()
        async with _claim(self._gate, _deadline(self.config.startup_timeout)):
            self._authorize()
            with self._files_gate:
                if not self.alive:
                    raise OSError("sandbox is retired")
                return self.outputs.stat_file(path, working_directory)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        if self.outputs is None:
            raise NotImplementedError("Hyperlight file channels are not enabled")
        self._authorize()
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        async with _claim(self._gate, _deadline(self.config.startup_timeout)):
            self._authorize()
            with self._files_gate:
                if not self.alive:
                    raise OSError("sandbox is retired")
                return self.outputs.read_file(
                    path,
                    working_directory,
                    min(max_bytes, DEFAULT_TRANSFER_LIMITS.max_bytes_per_file),
                )

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        if self.outputs is None:
            raise NotImplementedError("Hyperlight file channels are not enabled")
        self._authorize()
        async with _claim(self._gate, _deadline(self.config.startup_timeout)):
            self._authorize()
            with self._files_gate:
                if not self.alive:
                    raise OSError("sandbox is retired")
                return self.outputs.list_dir(path, working_directory)

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        raise NotImplementedError("Hyperlight file channels are not enabled")

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        raise NotImplementedError("Hyperlight file channels are not enabled")


class HyperlightSandboxBackend:
    """The packaged Python guest on WHP or KVM, shared by key/kind within this host process.

    Construction starts no worker. Acquire verifies the host and prepares a fresh reset baseline.
    Backend objects share a registry. The host's lock namespace permits one owning process.
    """

    name = BACKEND_NAME
    isolation = Isolation.MICROVM
    declarations = BackendDeclarations(
        capabilities=frozenset({Capability.RUN_CODE, Capability.SNAPSHOT}),
        egress_modes=frozenset({Egress.CLOSED, Egress.ALLOWLIST}),
        requires_exclusive_admission=True,
    )
    _gate: ClassVar[threading.Lock] = threading.Lock()
    _sandboxes: ClassVar[dict[tuple[SandboxKey, str], _HyperlightSandbox]] = {}

    def __init__(self, config: HyperlightSandboxConfig | None = None) -> None:
        self.config = config if config is not None else HyperlightSandboxConfig()
        self._owner = uuid.uuid4().hex
        if self.config.file_outputs:
            self.declarations = BackendDeclarations(
                capabilities=self.declarations.capabilities
                | {Capability.FILES_OUT, Capability.FILES_LIST},
                egress_modes=self.declarations.egress_modes,
                requires_exclusive_admission=True,
            )

    @asynccontextmanager
    async def call_admission(
        self, key: SandboxKey, spec: SandboxSpec, *, owner: str, timeout: float
    ) -> AsyncGenerator[AbstractContextManager[None]]:
        """Hold instance ownership through execution, output delivery and cleanup.

        Routers enter this automatically. Direct file-enabled users must enter it explicitly.
        """
        async with admit(key, spec.kind, owner=owner, timeout=timeout) as cleanup_authority:
            yield cleanup_authority

    def _targets(self, spec: SandboxSpec) -> tuple[str, ...]:
        missing = spec.required_capabilities - self.declarations.capabilities
        if missing:
            raise SandboxCapabilityNotSupported(f"Hyperlight cannot serve {sorted(missing)}")
        if spec.requires_os_family is not None or spec.host_tools is not None:
            raise ValueError("Hyperlight supplies no guest OS or host tools")
        if spec.image is not None or spec.image_id is not None:
            raise ValueError("the packaged runtime requires image=None, image_id=None")
        if spec.work_dir is not None and not (
            self.config.file_outputs and spec.work_dir == GUEST_ROOT
        ):
            raise ValueError("work_dir must be None, or /output with file_outputs enabled")
        if spec.isolation_scope not in self.declarations.isolation_scopes:
            raise ValueError("Hyperlight currently supports conversation isolation only")
        if spec.egress not in self.declarations.egress_modes:
            raise ValueError("Hyperlight supports CLOSED or ALLOWLIST egress")
        hosts: set[str] = set()
        for entry in spec.egress_allow:
            if isinstance(entry, EgressRule):
                if entry.methods is not None or entry.authority is not None:
                    raise ValueError("Hyperlight does not support refined egress rules")
                host = entry.host
            else:
                host = entry
            if host.startswith("*."):
                raise ValueError("Hyperlight requires exact egress hosts, without wildcards")
            hosts.add(host.lower())
        if len(hosts) > 512:
            raise ValueError("Hyperlight supports at most 512 egress hosts")
        return tuple(
            f"{scheme}://{host}/" for host in sorted(hosts) for scheme in ("http", "https")
        )

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        targets = self._targets(spec)
        if self.config.file_outputs:
            require_owner(key, spec.kind)
        check_host()
        deadline = _deadline(self.config.startup_timeout)
        async with _claim(self._gate, deadline):
            if self.config.file_outputs:
                require_owner(key, spec.kind)
            index = (key, spec.kind)
            previous = self._sandboxes.get(index)
            if previous is not None:
                if previous.alive:
                    if (previous.config, previous.targets, previous.contract) != (
                        self.config,
                        targets,
                        spec.execution_contract,
                    ):
                        raise ValueError("dispose the sandbox before changing its execution policy")
                    return previous
                await previous.stop()
                del self._sandboxes[index]
            sandbox = _HyperlightSandbox(
                self.config, targets, spec.execution_contract, self._owner, key, spec.kind
            )
            self._sandboxes[index] = sandbox
            try:
                await sandbox.prepare(deadline)
            except BaseException:
                await sandbox.stop()
                del self._sandboxes[index]
                raise
            return sandbox

    async def _dispose(
        self, key: SandboxKey, kind: str | None, instance_id: str | None
    ) -> tuple[int, DisposalFailure | None]:
        disposed = 0
        failures: list[DisposalFailure] = []
        for index, sandbox in tuple(self._sandboxes.items()):
            if index[0] != key or (kind is not None and index[1] != kind):
                continue
            if sandbox.outputs is not None:
                try:
                    require_owner(key, index[1], allow_idle=True)
                except RuntimeError:
                    failures.append(DisposalFailure("unknown", "sandbox has an active file call"))
                    continue
            if not sandbox.retire(instance_id):
                continue
            try:
                await sandbox.stop()
            except Exception as error:
                failures.append(DisposalFailure("unknown", str(error)))
            else:
                del self._sandboxes[index]
                disposed += 1
        return disposed, fold_disposal_failures(failures)

    async def dispose(
        self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
    ) -> DisposalFailure | None:
        try:
            check_host()
            async with _claim(self._gate, _deadline(self.config.startup_timeout)):
                _, failure = await self._dispose(key, kind, instance_id)
                return failure
        except Exception as error:
            return DisposalFailure("unknown", str(error))

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        count = 0
        failures: list[DisposalFailure] = []
        try:
            check_host()
            async with _claim(self._gate, _deadline(self.config.startup_timeout)):
                keys = {
                    key
                    for key, _ in self._sandboxes
                    if key.scope == scope and key.thread_id == thread_id
                }
                for key in keys:
                    disposed, failure = await self._dispose(key, None, None)
                    count += disposed
                    if failure is not None:
                        failures.append(failure)
        except Exception as error:
            failures.append(DisposalFailure("unknown", str(error)))
        return ScopePurge(count, fold_disposal_failures(failures))

    async def aclose(self) -> None:
        """Dispose the sandboxes this backend created, retaining any failed targets for retry."""
        failures: list[DisposalFailure] = []
        async with _claim(self._gate, _deadline(self.config.startup_timeout)):
            for (key, kind), sandbox in tuple(self._sandboxes.items()):
                if sandbox.owner == self._owner:
                    _, failure = await self._dispose(key, kind, sandbox.instance_id)
                    if failure is not None:
                        failures.append(failure)
        if failure := fold_disposal_failures(failures):
            raise HyperlightWorkerError(str(failure))


if TYPE_CHECKING:
    _binding: tuple[SandboxBackend, type[Sandbox]] = (
        HyperlightSandboxBackend(),
        _HyperlightSandbox,
    )
