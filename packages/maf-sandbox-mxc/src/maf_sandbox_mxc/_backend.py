"""Closed-network MXC backend with journaled calls and optional checkpoint recovery."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import threading
from collections.abc import AsyncGenerator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, asynccontextmanager
from typing import TYPE_CHECKING, cast

from maf_sandbox import (
    BackendDeclarations,
    Capability,
    DisposalFailure,
    Egress,
    ExecResult,
    Isolation,
    IsolationScope,
    Sandbox,
    SandboxBackend,
    SandboxCapabilityNotSupported,
    SandboxEntry,
    SandboxKey,
    SandboxLimits,
    SandboxSpec,
    ScopePurge,
    TransferLimits,
)

from ._admission import Admission
from ._channel import MxcProgramChannel
from ._config import MxcSandboxConfig
from ._files import GUEST_ROOT, Files
from ._state import Catalog
from .files.request import Request, name
from .files.transport import GuestProgramError
from .host_store import Refused


async def _finish[T](future: asyncio.Future[T]) -> T:
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


def _timeout(value: float) -> None:
    if not math.isfinite(value) or not 0 < value <= 90:
        raise ValueError("MXC requires a positive timeout of at most 90 seconds")


class MxcSandbox:
    """A durable session handle; file operations require its exclusive admission scope."""

    def __init__(
        self,
        backend: MxcSandboxBackend,
        key: SandboxKey,
        spec: SandboxSpec,
        session: str,
        fresh: bool,
    ):
        self.backend = backend
        self.key = key
        self.spec = spec
        self.instance_id = session
        self.freshly_created = fresh
        self.files = Files(backend.config.files)
        self.alive = True
        self.busy = False
        self.cancel: threading.Event | None = None
        self.last_request: tuple[str, Request, int] | None = None

    def authorize(self) -> None:
        self.backend.admission.require((self.key, self.spec.kind))
        if not self.alive or self.backend.closed:
            raise Refused("MXC sandbox is retired")
        if self.busy:
            raise Refused("MXC sandbox already has a running program")

    async def run_call(
        self, call_id: str, code: str, *, guest_call_path: str, timeout: float
    ) -> ExecResult:
        """Replay the exact committed call identity; a new identity executes a new program."""
        self.authorize()
        _timeout(timeout)
        name(guest_call_path)
        if "/" in guest_call_path:
            raise ValueError("MXC requires one call directory component")
        request = Request(
            code.encode(),
            self.files.inputs(guest_call_path),
            (),
            self.backend.config.files,
            self.backend.config.workspace,
            guest_call_path,
        )
        if self.last_request is not None:
            previous_id, previous_request, revision = self.last_request
            if previous_id == call_id and revision == self.files.revision:
                if (
                    previous_request.code != request.code
                    or previous_request.call_directory != guest_call_path
                ):
                    raise Refused("retry code or call directory differs")
                request = previous_request
        self.busy = True
        self.cancel = cancel = threading.Event()
        try:
            try:
                result = await self.backend.work(
                    lambda catalog: catalog.execute(
                        self.instance_id, call_id, request, cancel, timeout
                    ),
                    timeout=timeout,
                    cancel=cancel,
                )
            except GuestProgramError as error:
                result = error.result
            else:
                self.files.publish(result)
                self.last_request = (call_id, request, self.files.revision)
            completion = json.loads(result)
            streams = completion["streams"]
            self.freshly_created = False
            return ExecResult(
                exit_code=completion.get("exit_code", 0),
                stdout_bytes=base64.b64decode(streams["stdout"]["base64"], validate=True),
                stderr_bytes=base64.b64decode(streams["stderr"]["base64"], validate=True),
            )
        finally:
            self.busy = False
            self.cancel = None

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        self.authorize()
        self.files.write(
            path, content.encode() if isinstance(content, str) else content, working_directory
        )

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        self.authorize()
        entry = self.files.stat(self.files.resolve(path, working_directory))
        return None if entry is None else SandboxEntry(path, entry.kind, entry.size_bytes)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        self.authorize()
        return self.files.read(path, working_directory, max_bytes)

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        self.authorize()
        return self.files.list(path, working_directory)

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        self.authorize()
        self.files.remove(path, working_directory, recursive)

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        self.authorize()
        _timeout(timeout)
        self.files.resolve(directory, working_directory, self_allowed=False)
        self.busy = True
        try:
            await self.backend.work(
                lambda catalog: catalog.reclaim(self.instance_id), timeout=timeout
            )
            self.files.remove(directory, working_directory, True)
        finally:
            self.busy = False

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        raise NotImplementedError("MXC executes programs through its native program channel")

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        raise NotImplementedError("use the MXC program channel to bind the call directory")

    async def reset(self, *, timeout: float) -> None:
        raise NotImplementedError("dispose MXC sessions to establish a fresh baseline")


class MxcSandboxBackend:
    """Single-process root ownership, with explicit initialization and host-owned native assets.

    Constructing starts no native process. Call initialize() once for a new root; existing
    roots reopen on acquire. Closing releases ownership without deleting durable sessions.
    """

    name = "mxc"
    isolation = Isolation.MICROVM

    def __init__(self, config: MxcSandboxConfig):
        self.config = config
        self.admission = Admission()
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mxc-owner")
        self.catalog: Catalog | None = None
        self.sandboxes: dict[tuple[SandboxKey, str], MxcSandbox] = {}
        limits = config.files
        self.declarations = BackendDeclarations(
            capabilities=frozenset(
                {
                    Capability.FILES_IN,
                    Capability.FILES_OUT,
                    Capability.FILES_LIST,
                    Capability.FILES_DELETE,
                    Capability.RECLAIM,
                }
            ),
            limits=SandboxLimits(
                files_in=TransferLimits(limits.file_bytes, limits.input_bytes, limits.input_files),
                files_out=TransferLimits(
                    limits.file_bytes, limits.artifact_bytes, limits.artifact_files
                ),
            ),
            egress_modes=frozenset({Egress.CLOSED}),
            isolation_scopes=frozenset({IsolationScope.CONVERSATION, IsolationScope.CALL}),
            requires_exclusive_admission=True,
            program_channels=(MxcProgramChannel(),),
            recovers_state=config.persistent,
        )

    async def initialize(self) -> None:
        """Provision a new private journal explicitly; never overwrite an existing root."""
        if self.closed or self.catalog is not None:
            raise Refused("backend is already open or closed")
        async with self.admission.enter("initialize", 90):
            await self.work(lambda _: None, initialize=True)

    async def work[T](
        self,
        operation: Callable[[Catalog], T],
        *,
        initialize: bool = False,
        timeout: float | None = None,
        cancel: threading.Event | None = None,
    ) -> T:
        if self.closed:
            raise Refused("backend is closed")

        def run() -> T:
            if self.catalog is None:
                self.catalog = Catalog(self.config, initialize=initialize)
            return operation(self.catalog)

        future = asyncio.wrap_future(self.executor.submit(run))
        try:
            done, _ = await asyncio.wait({future}, timeout=timeout)
            if not done:
                if cancel is not None:
                    cancel.set()
                try:
                    await _finish(future)
                except Exception:
                    pass
                raise TimeoutError("MXC call deadline expired; native owner has stopped")
            return future.result()
        except asyncio.CancelledError:
            if cancel is not None:
                cancel.set()
            try:
                await _finish(future)
            except Exception:
                pass
            raise

    @asynccontextmanager
    async def call_admission(
        self, key: SandboxKey, spec: SandboxSpec, *, owner: str, timeout: float
    ) -> AsyncGenerator[AbstractContextManager[None]]:
        """Hold root ownership until output delivery and cleanup have completed."""
        if not owner:
            raise ValueError("call admission requires an owner")
        self.validate(spec)
        async with self.admission.enter((key, spec.kind), timeout) as cleanup:
            yield cleanup

    def validate(self, spec: SandboxSpec) -> None:
        """Reject unsupported authority before opening a journal or starting a helper."""
        if spec.egress is not Egress.CLOSED or spec.egress_allow:
            raise ValueError("MXC supports CLOSED networking only; allowlisting is not qualified")
        missing = spec.required_capabilities - self.declarations.capabilities
        if missing:
            raise SandboxCapabilityNotSupported(f"MXC cannot serve {sorted(missing)}")
        if (
            spec.image is not None
            or spec.image_id is not None
            or spec.requires_os_family is not None
        ):
            raise ValueError("MXC uses explicitly pinned runtime assets, not a guest OS image")
        if spec.host_tools is not None or spec.work_dir not in (None, GUEST_ROOT):
            raise ValueError("MXC requires its allocated storage base and no host tools")
        if spec.program is not None:
            self.declarations.program_channels[0].transfer_limits(spec)
        for capability, requested, admitted in (
            (Capability.FILES_IN, spec.files_in, self.declarations.limits.files_in),
            (Capability.FILES_OUT, spec.files_out, self.declarations.limits.files_out),
        ):
            if capability not in spec.required_capabilities:
                continue
            if (
                requested.max_files > admitted.max_files
                or requested.max_total_bytes > admitted.max_total_bytes
                or requested.max_bytes_per_file > admitted.max_bytes_per_file
            ):
                raise ValueError("requested transfers exceed configured MXC limits")

    async def replay_result(
        self,
        key: SandboxKey,
        spec: SandboxSpec,
        *,
        instance_id: str,
        call_id: str,
        request: Request,
    ) -> bytes:
        """Return the retained result envelope for an exact request, including retired sessions.

        Requires matching call admission. No native assets are needed and an unknown,
        interrupted or expired call is refused without admitting a new execution.
        """
        self.validate(spec)
        self.admission.require((key, spec.kind))
        return await self.work(
            lambda catalog: catalog.replay(key, spec, instance_id, call_id, request)
        )

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> MxcSandbox:
        self.validate(spec)
        self.admission.require((key, spec.kind))
        index = (key, spec.kind)
        session, fresh = await self.work(lambda catalog: catalog.acquire(key, spec))
        previous = self.sandboxes.get(index)
        if previous is not None and previous.instance_id == session:
            previous.freshly_created = False
            return previous
        sandbox = MxcSandbox(self, key, spec, session, fresh)
        self.sandboxes[index] = sandbox
        return sandbox

    async def dispose(
        self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
    ) -> DisposalFailure | None:
        try:
            if self.catalog is None and not (self.config.root / "shared.sqlite").exists():
                return None
            if self.admission.permit is None:
                async with self.admission.enter(("dispose", key), 90):
                    await self._retire(key, kind, instance_id)
            else:
                if kind is None:
                    raise Refused("disposal of all kinds requires idle root ownership")
                self.admission.require((key, kind))
                await self._retire(key, kind, instance_id)
            return None
        except Exception as error:
            return DisposalFailure("unknown", str(error))

    async def _retire(self, key: SandboxKey, kind: str | None, instance_id: str | None) -> None:
        retained = await self.work(lambda catalog: catalog.key_sessions(key, kind))
        for session in retained:
            if instance_id is not None and session != instance_id:
                continue
            current = next((s for s in self.sandboxes.values() if s.instance_id == session), None)
            if current is not None:
                if current.busy:
                    raise Refused("wait for the active call to stop before disposal")
                current.alive = False
            await self.work(lambda catalog: catalog.retire(session))
            if current is not None:
                del self.sandboxes[current.key, current.spec.kind]

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        disposed = 0
        try:
            async with self.admission.enter("purge", 90):
                sessions = await self.work(lambda catalog: catalog.scope_sessions(scope, thread_id))
                for session in sessions:
                    await self.work(lambda catalog: catalog.retire(session))
                    for index, sandbox in tuple(self.sandboxes.items()):
                        if sandbox.instance_id == session:
                            sandbox.alive = False
                            del self.sandboxes[index]
                    disposed += 1
            return ScopePurge(disposed)
        except Exception as error:
            return ScopePurge(disposed, DisposalFailure("unknown", str(error)))

    async def aclose(self) -> None:
        """Finish admitted work and release ownership, preserving durable state for recovery."""
        if self.closed:
            return
        async with self.admission.enter("close", 90):
            if self.catalog is not None:
                await self.work(lambda catalog: catalog.close())
            self.closed = True
            for sandbox in self.sandboxes.values():
                sandbox.alive = False
            self.sandboxes.clear()
            self.executor.shutdown(wait=True)


if TYPE_CHECKING:
    _binding: tuple[SandboxBackend, type[Sandbox]] = (
        MxcSandboxBackend(cast(MxcSandboxConfig, None)),
        MxcSandbox,
    )
