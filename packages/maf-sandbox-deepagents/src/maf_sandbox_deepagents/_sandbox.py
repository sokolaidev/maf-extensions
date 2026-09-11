"""The adapter: a ``maf_sandbox`` router behind Deep Agents' ``BaseSandbox``.

Deep Agents hands an agent one ``execute`` tool over a sandbox object the host constructs, and
derives its file tools from ``execute`` and ``upload_files``. This module implements that
object over a :class:`~maf_sandbox.SandboxRouter`: the router still refuses a backend below
the host's isolation floor or one that cannot enforce the spec's egress mode, and the sandbox
is still keyed from the host's request context and purged with the conversation.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import math
import os
import posixpath
import shlex
import threading
import time
import uuid
from collections.abc import Coroutine
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from maf_sandbox import (
    DEFAULT_TRANSFER_LIMITS,
    BoundedExec,
    Capability,
    Cleanup,
    Egress,
    EgressRule,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    NoSandboxBackend,
    Sandbox,
    SandboxExecOutputLimitExceeded,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
)
from maf_sandbox.paths import posix_work_dir_ancestors

__all__ = [
    "DEEPAGENTS_KIND",
    "DEFAULT_EXEC_TIMEOUT_SECONDS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_WORK_DIR",
    "REQUIRED_CAPABILITIES",
    "SANDBOX_UNAVAILABLE",
    "STORAGE_BASE",
    "MafSandbox",
    "deepagents_spec",
]

logger = logging.getLogger(__name__)

#: The ``kind`` a Deep Agents sandbox is keyed under. Part of the sandbox's identity, so an
#: agent's shell never shares a container with a packaged kind serving the same conversation.
DEEPAGENTS_KIND = "deepagents"

#: What Deep Agents needs from a backend: a shell, files pushed in for ``upload_files`` and
#: ``write_file``, files pulled out for ``download_files``. The other derived tools run on
#: ``execute`` alone, and ``write_file`` runs a Python preflight there before it uploads.
REQUIRED_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}
)

DEFAULT_EXEC_TIMEOUT_SECONDS = 120.0

#: The most combined stdout and stderr one command may return, in bytes. Above the 500 KiB
#: page Deep Agents' own ``read_file`` renders, so a large read still comes back whole; a
#: command past it is refused rather than truncated, which is the suite's bounded-exec contract.
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576

#: The protocol's default base, read off the spec rather than spelled a second time here.
DEFAULT_WORK_DIR: str | None = SandboxSpec(kind=DEEPAGENTS_KIND).work_dir

#: The acquired sandbox's storage base, addressed relatively: every command runs there and
#: every upload and download path is relative to it. The backend resolves it to the base the
#: spec named; a spec that names none is refused at construction.
STORAGE_BASE = "."

#: What the model reads when the router or the backend could not serve a call. Fixed, because
#: the provider's own message can carry an endpoint or a tenant and a tool result is persisted
#: into the transcript; the detail goes to the log.
SANDBOX_UNAVAILABLE = "Error: sandbox unavailable — the command did not run."

#: Says only what every backend can establish: the wait ended. Whether the command was
#: stopped is the backend's own contract, and differs between them.
_TIMED_OUT = "Error: the command did not finish within {seconds:g} seconds."
#: Distinct from :data:`SANDBOX_UNAVAILABLE`: the command may have run, and the result is what
#: did not come back.
_EXEC_FAILED = "Error: the sandbox did not return the command's result; see the host log."
#: The whole output is dropped, not cut: the backend refuses past the budget and returns none
#: of it, so a partial result is never mistaken for a program that printed less.
_OUTPUT_DROPPED = "Error: the command's output exceeded {limit} bytes and was dropped."
_UNBOUNDED = (
    "Error: sandbox unavailable — the backend cannot bound the command's output, so the "
    "command did not run."
)
_UPLOAD_FAILED = "upload failed; see the host log"
#: A shell transfer that did not finish — a timeout, an output overflow, the caller leaving —
#: condemns the sandbox, and everything the batch had put there goes with it. Condemned, not
#: disposed: the delete is queued with the router and may still be pending, or fail.
_UPLOAD_BATCH_LOST = (
    "the sandbox is condemned after a transfer did not finish; the batch did not land"
)
_DOWNLOAD_BATCH_LOST = (
    "the sandbox is condemned after a transfer did not finish; the rest of the batch was not read"
)
_DOWNLOAD_FAILED = "download failed; see the host log"
_SIZE_UNKNOWN = "the sandbox could not report the file's size"
_TOO_MANY_FILES = "the batch has more files than {direction}.max_files allows"
_NO_SHELL_ROAD = (
    "the path is outside the storage base and the backend cannot run a bounded command to reach it"
)

#: Raw bytes per command on the shell road. Base64 of it is 64 KiB, under the 128 KiB Linux
#: allows one argument, and ``sh -c`` receives the whole command as one.
_SHELL_CHUNK_BYTES = 48 * 1024
_OVER_FILE_CAP = "the file is larger than {direction}.max_bytes_per_file"
_OVER_TOTAL_CAP = "the batch would exceed {direction}.max_total_bytes"


def deepagents_spec(
    image: str | None = None,
    *,
    image_id: str | None = None,
    egress_allow: tuple[str | EgressRule, ...] = (),
    work_dir: str | None = DEFAULT_WORK_DIR,
    files_in: TransferLimits = DEFAULT_TRANSFER_LIMITS,
    files_out: TransferLimits = DEFAULT_TRANSFER_LIMITS,
    min_isolation: Isolation | None = None,
    kind: str = DEEPAGENTS_KIND,
) -> SandboxSpec:
    """The spec a Deep Agents sandbox asks for.

    Egress is derived, never passed: named hosts run :data:`~maf_sandbox.Egress.ALLOWLIST` with
    those hosts as the payload, and none runs :data:`~maf_sandbox.Egress.CLOSED`. The open
    posture is not expressible, because the agent writes the shell commands this sandbox runs.

    ``work_dir`` names the base the agent's file paths resolve under. It is the one field of
    the spec :class:`MafSandbox` refuses ``None`` for.
    """
    return SandboxSpec(
        kind=kind,
        image=image,
        image_id=image_id,
        egress_allow=tuple(egress_allow),
        egress=Egress.ALLOWLIST if egress_allow else Egress.CLOSED,
        work_dir=work_dir,
        requires=REQUIRED_CAPABILITIES,
        files_in=files_in,
        files_out=files_out,
        min_isolation=min_isolation,
    )


class _SyncRunner:
    """One loop on a thread of its own, shared by every adapter in the process, for the
    synchronous surface.

    Deep Agents calls that surface from sync tools, which LangGraph runs on a worker thread
    with no loop, and a caller holding a running loop cannot nest another. One loop for the
    process rather than one per adapter or per call, because a backend may cache a client per
    loop (ACAS does) and never evicts one for a loop that closed; a loop that lives with the
    process leaves it exactly one. Started on the first sync call; the thread is a daemon.
    A fork carries the loop into the child but not its thread, so the child starts over on
    its first sync call, under a fresh guard: the inherited one may be held by a thread that
    did not cross.
    """

    _THREAD_NAME = "maf-sandbox-deepagents"

    def __init__(self) -> None:
        self._reset()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset)

    def _reset(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def _started(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name=self._THREAD_NAME, daemon=True
                )
                self._thread.start()
            return self._loop

    def run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        return self.submit(coroutine).result()

    def submit[T](self, coroutine: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Run ``coroutine`` on the loop and hand back its future: work that must outlive the
        caller's loop, joinable from any loop."""
        return asyncio.run_coroutine_threadsafe(coroutine, self._started())


_SYNC = _SyncRunner()


def _response(result: ExecResult, *, max_output_bytes: int | None = None) -> ExecuteResponse:
    """One combined stream, the way Deep Agents' own backends render it.

    Each ``stderr`` line is prefixed so the model can tell the two apart, and the prefix says
    whose words they are: a producer that took the field is speaking there, not the program.
    The prefixes grow the stream, so it is held to ``max_output_bytes`` again once rendered:
    the budget is what reaches the model, and a stream of short lines must not step over it.
    """
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        label = "[note]" if result.producer_owns_stderr else "[stderr]"
        parts.extend(f"{label} {line}" for line in result.stderr.strip().splitlines())
    output = "\n".join(parts) if parts else "<no output>"
    if max_output_bytes is not None and len(output.encode("utf-8")) > max_output_bytes:
        return ExecuteResponse(
            output=_OUTPUT_DROPPED.format(limit=max_output_bytes), exit_code=None, truncated=True
        )
    return ExecuteResponse(output=output, exit_code=result.exit_code, truncated=False)


@dataclasses.dataclass
class _Call:
    """One admitted operation: its owner token, its admission, and the sandbox it acquired."""

    owner: str
    admission: Any
    sandbox: Sandbox | None = None
    condemned: bool = False


class _BatchLost(Exception):
    """The sandbox went in the middle of a batch; ``response`` answers the file that took it."""

    def __init__(self, response: FileDownloadResponse | None = None) -> None:
        super().__init__()
        self.response = response


def _shell_error(stderr: str) -> str:
    """The Deep Agents code for a failed shell write, read off what the shell said."""
    if "Permission denied" in stderr:
        return PERMISSION_DENIED
    if "Is a directory" in stderr:
        return IS_DIRECTORY
    return INVALID_PATH


class MafSandbox(BaseSandbox):
    """A :class:`~maf_sandbox.SandboxRouter` as a Deep Agents sandbox.

    One instance serves one conversation: ``key`` is the host's scope, thread and agent
    directory, read from the host's request context and never from the model. The sandbox is
    acquired on first use — get-or-create, so it stays warm across turns — and lives until the
    host disposes it: :meth:`aclose` here, or the router's ``dispose_scope`` on the host's own
    conversation-delete path, which is what every other sandbox in the suite answers to.

    Construction refuses what the router refuses — no backend at all, one below the isolation
    floor, one that cannot enforce the spec's egress mode, a missing capability — so a
    misconfigured host fails before an agent is built, not on its first command.

    Paths are guest paths, as they are for every sandbox Deep Agents ships: the agent's file
    tools name them absolutely. Inside ``spec.work_dir`` the adapter's upload and download go
    through the backend's file plane; outside it, where Deep Agents keeps its offloaded history
    and its large-edit temporaries, they go through the shell the agent already has, in base64
    chunks, under the same caps. The host puts ``spec.work_dir`` in the prompt so the model
    knows where its own files are; a spec leaving the base to the backend is refused, because
    nothing could then tell the model.

    Deep Agents' derived file tools (``ls``, ``read_file``, ``write_file``, ``edit_file``,
    ``glob``, ``grep``) run ``python3`` inside the guest, ``write_file`` for the preflight that
    creates the parent directory before it uploads; on an image without it only ``execute``,
    ``delete`` and this class's own upload and download work. The image is the host's to choose.

    Every command runs under ``max_output_bytes``, enforced by the backend before it buffers
    the output: the model writes the command, so what it prints is bounded on the host or the
    command does not run.
    """

    def __init__(
        self,
        router: SandboxRouter,
        key: SandboxKey,
        spec: SandboxSpec,
        *,
        exec_timeout_seconds: float = DEFAULT_EXEC_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        missing = REQUIRED_CAPABILITIES - spec.requires
        if missing:
            raise ValueError(
                f"the {spec.kind!r} spec does not require {sorted(str(c) for c in missing)}, "
                "which Deep Agents' execute, write and download tools need; build it with "
                "deepagents_spec()"
            )
        if spec.isolation_scope is not IsolationScope.CONVERSATION:
            raise ValueError(
                "a Deep Agents sandbox serves a whole conversation, so the spec cannot ask for "
                f"isolation_scope={spec.isolation_scope!r}"
            )
        if key.call_id:
            raise ValueError("key.call_id must be empty: one sandbox serves the conversation")
        if spec.egress is Egress.UNRESTRICTED:
            # `deepagents_spec` cannot express it; a spec built another way must not either.
            raise ValueError(
                "spec.egress must be CLOSED or ALLOWLIST: the model writes the commands, so an "
                "open network is not a posture this sandbox takes"
            )
        if spec.work_dir is None:
            # Deep Agents' file tools take guest paths the model spells out, so the host has to
            # be able to tell it the base; a backend-allocated one is knowable to neither.
            raise ValueError(
                "spec.work_dir must name the storage base: Deep Agents addresses files by "
                "guest path, and a base the backend allocates is one nothing can tell the model"
            )
        # The backends' own rule for a named base, applied here so a relative, empty or
        # NUL-bearing base is a host configuration error at construction, not a sandbox
        # unavailable on the first command.
        posix_work_dir_ancestors(spec.work_dir)
        if not math.isfinite(exec_timeout_seconds) or exec_timeout_seconds <= 0:
            raise ValueError("exec_timeout_seconds must be a finite positive number of seconds")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer of bytes")
        if not router.enabled:
            raise NoSandboxBackend("no sandbox backend is configured")
        router.ensure_can_serve(spec)
        self._router = router
        self._key = key
        self._spec = spec
        self._timeout = float(exec_timeout_seconds)
        self._max_output_bytes = max_output_bytes
        #: The engine instance the last acquire handed back, and what `aclose` deletes.
        self._instance_id: str | None = None
        backend = router.backend_for(spec)
        identity = [
            "" if backend is None else backend.name,
            key.scope,
            key.thread_id,
            key.agent_dir,
            spec.kind,
            str(spec.egress),
            sorted(str(entry) for entry in spec.egress_allow),
        ]
        digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        # The id names the sandbox, as a provider's own id would: two adapters over one key,
        # kind, backend and egress posture reach one sandbox through the router's get-or-create,
        # and say so; a backend keys a container on the egress posture too. Opaque rather than
        # the key spelled out, because Deep Agents may render it to the model and a scope is
        # often a tenant or a user.
        self._id = f"maf-sandbox-{digest[:16]}"

    @property
    def id(self) -> str:
        return self._id

    @property
    def key(self) -> SandboxKey:
        """What this sandbox is keyed by."""
        return self._key

    @property
    def spec(self) -> SandboxSpec:
        """The sandbox this instance asks the router for."""
        return self._spec

    @property
    def router(self) -> SandboxRouter:
        """The router serving this sandbox."""
        return self._router

    async def _open(self, bound: float) -> tuple[_Call, Sandbox] | None:
        """Admit a call and acquire its sandbox under one deadline.

        Admission is the router's call lifecycle, shared by every call over this key from
        this adapter or another: a delete one call queued runs when the last of them leaves,
        and a new call waits for it, so nothing in flight is cut off and nothing reaches an
        instance being deleted. ``None`` when the sandbox is unavailable, with the reason in
        the log; ``TimeoutError`` when the deadline passed.
        """
        owner = uuid.uuid4().hex
        call: _Call | None = None
        try:
            async with asyncio.timeout(bound):
                try:
                    admission = await self._router.enter_call(
                        self._key, self._spec, owner=owner, timeout=bound
                    )
                except TimeoutError:
                    raise
                except Exception:
                    logger.exception("%s: the call was not admitted", self._id)
                    return None
                call = _Call(owner, admission)
                try:
                    sandbox = await self._router.acquire(
                        self._key, self._spec, _admission=admission
                    )
                except asyncio.CancelledError:
                    # The deadline, or the caller leaving: the admission must not outlive
                    # the call, or an exclusive close and every queued delete wait on it.
                    await self._leave(call, deferred=True)
                    call = None
                    raise
                except Exception:
                    logger.exception("%s: the sandbox could not be acquired", self._id)
                    await self._leave(call)
                    return None
        except TimeoutError:
            if call is not None:
                await self._leave(call, deferred=True)
            raise
        self._instance_id = sandbox.instance_id
        call.sandbox = sandbox
        return call, sandbox

    def _condemn(self, call: _Call) -> None:
        """Queue the instance's delete with the router, for when the last call over this key
        leaves. `aclose` keeps naming the instance: a delete that failed is retried there, and
        one that landed makes that a no-op under the protocol."""
        if call.condemned or call.sandbox is None:
            return
        call.condemned = True
        self._router.queue_cleanup(
            self._key,
            self._spec,
            admission=call.admission,
            sandbox=call.sandbox,
            owner=call.owner,
            rung=Cleanup.DISPOSE,
        )

    async def _leave(self, call: _Call, *, deferred: bool = False) -> None:
        """Release the admission; the last call out runs whatever delete was queued.

        After an unfinished command that release goes to the process's own loop, so the
        delete never extends the caller's wait and outlives a loop ``asyncio.run`` closes on
        return; the next call over the key is admitted once it is done.
        """
        release: Coroutine[Any, Any, None] = self._router.release_call(
            self._key, self._spec.kind, owner=call.owner
        )
        if not (call.condemned or deferred):
            await release
            return
        # `_SyncRunner.submit` takes the coroutine itself and runs it on the process's loop.
        released = _SYNC.submit(release)
        released.add_done_callback(self._left)

    def _left(self, future: concurrent.futures.Future[None]) -> None:
        if future.cancelled():
            logger.warning("%s: the release after an unfinished command was cancelled", self._id)
        elif (failure := future.exception()) is not None:
            logger.error(
                "%s: the release after an unfinished command failed: %s", self._id, failure
            )

    async def _run_bounded(
        self, call: _Call, command: str, *, timeout: float, max_output_bytes: int
    ) -> ExecResult:
        """``exec_bounded``, with the instance condemned whenever the command's end is unknown.

        A timeout says the wait ended, an overflow that the host stopped reading, a
        cancellation that the caller left, and any other failure that the result did not come
        back: none says the guest process stopped, and the backends do not establish it
        either, so the instance goes before anything reuses it.
        """
        sandbox = call.sandbox
        assert isinstance(sandbox, BoundedExec)
        try:
            return await sandbox.exec_bounded(
                command,
                working_directory=STORAGE_BASE,
                timeout=timeout,
                max_output_bytes=max_output_bytes,
            )
        except BaseException:
            self._condemn(call)
            raise

    def _inside_base(self, path: str) -> bool:
        """Whether ``path`` names something under the storage base, the file plane's reach."""
        if not path.startswith("/"):
            return True
        # Both sides normalized: a spec may spell the base with `..`, and the backends resolve
        # it before they confine to it.
        base = PurePosixPath(posixpath.normpath(self._spec.work_dir or "/"))
        normalized = PurePosixPath(posixpath.normpath(path))
        return normalized == base or base in normalized.parents

    # --- execute ---------------------------------------------------------------------------

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            return ExecuteResponse(output="Error: Command must be a non-empty string.", exit_code=1)
        bound = self._timeout if timeout is None else float(timeout)
        if not math.isfinite(bound) or bound <= 0:
            raise ValueError(f"timeout must be a finite positive number of seconds, got {timeout}")
        # One deadline over the acquire and the command: a cold create spends part of the
        # budget Deep Agents defines as the wait for the command, is cut off if it outlives
        # it, and the command gets the rest.
        started = time.monotonic()
        try:
            opened = await self._open(bound)
        except TimeoutError:
            return ExecuteResponse(output=_TIMED_OUT.format(seconds=bound), exit_code=None)
        if opened is None:
            return ExecuteResponse(output=SANDBOX_UNAVAILABLE, exit_code=None)
        call, sandbox = opened
        try:
            if not isinstance(sandbox, BoundedExec):
                logger.error("%s: the backend has no exec_bounded, so no command runs", self._id)
                return ExecuteResponse(output=_UNBOUNDED, exit_code=None)
            remaining = bound - (time.monotonic() - started)
            if remaining <= 0:
                return ExecuteResponse(output=_TIMED_OUT.format(seconds=bound), exit_code=None)
            try:
                result = await self._run_bounded(
                    call, command, timeout=remaining, max_output_bytes=self._max_output_bytes
                )
            except TimeoutError:
                return ExecuteResponse(output=_TIMED_OUT.format(seconds=bound), exit_code=None)
            except SandboxExecOutputLimitExceeded:
                return ExecuteResponse(
                    output=_OUTPUT_DROPPED.format(limit=self._max_output_bytes),
                    exit_code=None,
                    truncated=True,
                )
            except Exception:
                logger.exception("%s: the command's result could not be read", self._id)
                return ExecuteResponse(output=_EXEC_FAILED, exit_code=None)
            return _response(result, max_output_bytes=self._max_output_bytes)
        finally:
            await self._leave(call)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return _SYNC.run(self.aexecute(command, timeout=timeout))

    # --- files in --------------------------------------------------------------------------

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        limits = self._spec.files_in
        if len(files) > limits.max_files:
            refusal = _TOO_MANY_FILES.format(direction="files_in")
            return [FileUploadResponse(path=path, error=refusal) for path, _ in files]
        try:
            opened = await self._open(self._timeout)
        except TimeoutError:
            opened = None
        if opened is None:
            return [FileUploadResponse(path=path, error=_UPLOAD_FAILED) for path, _ in files]
        call, sandbox = opened
        try:
            responses: list[FileUploadResponse] = []
            sent = 0
            for path, content in files:
                # Checked before the write it would have prevented, and counted only for what
                # crossed: a refused file leaves the budget where it was.
                error: str | None
                try:
                    if len(content) > limits.max_bytes_per_file:
                        error = _OVER_FILE_CAP.format(direction="files_in")
                    elif sent + len(content) > limits.max_total_bytes:
                        error = _OVER_TOTAL_CAP.format(direction="files_in")
                    elif self._inside_base(path):
                        error = await self._upload_via_plane(call, sandbox, path, content)
                    elif isinstance(sandbox, BoundedExec):
                        error = await self._upload_via_shell(call, path, content)
                    else:
                        error = _NO_SHELL_ROAD
                except _BatchLost:
                    return [FileUploadResponse(path=p, error=_UPLOAD_BATCH_LOST) for p, _ in files]
                if error is None:
                    sent += len(content)
                responses.append(FileUploadResponse(path=path, error=error))
            return responses
        finally:
            await self._leave(call)

    async def _upload_via_plane(
        self, call: _Call, sandbox: Sandbox, path: str, content: bytes
    ) -> str | None:
        """Write under the base through the file plane; the error code, or ``None``.

        A write the plane did not refuse and did not finish — a transport failure, the caller
        leaving — may have left part of the file, and the plane does not promise otherwise, so
        the instance is condemned and the batch ends, as on the shell road.
        """
        try:
            await sandbox.write_file(path, content, working_directory=STORAGE_BASE)
        except PermissionError as refused:
            logger.info("%s: upload of %r refused: %s", self._id, path, refused)
            return PERMISSION_DENIED
        except (ValueError, NotADirectoryError) as refused:
            # The file plane's own refusals — through a link, a parent that is a file — are
            # the guest's shape, safe to name by code.
            logger.info("%s: upload of %r refused: %s", self._id, path, refused)
            return INVALID_PATH
        except asyncio.CancelledError:
            self._condemn(call)
            raise
        except Exception:
            logger.exception("%s: upload of %r failed", self._id, path)
            self._condemn(call)
            raise _BatchLost() from None
        return None

    async def _upload_via_shell(self, call: _Call, path: str, content: bytes) -> str | None:
        """Write outside the base through the shell the agent already has.

        Deep Agents writes its offloaded history under ``/conversation_history`` and its
        large-edit temporaries under ``/tmp``, which the file plane, confined to the base,
        cannot reach; ``execute`` can, so this widens nothing. The caps were applied by the
        caller. Base64 in chunks, because a command is one argument to ``sh -c``; the chunks
        land in a sibling of the target named for this call alone, moved into place once the
        last has, so a reader, or a second writer over the same path, sees a whole file and
        never an interleaving of two.
        """
        target = shlex.quote(path)
        parent = shlex.quote(posixpath.dirname(path) or "/")
        staged = shlex.quote(f"{path}.{uuid.uuid4().hex}.part")
        encoded = base64.b64encode(content).decode("ascii")
        step = 4 * (_SHELL_CHUNK_BYTES // 3)
        commands = [
            f"mkdir -p {parent} && if [ -d {target} ]; then echo 'Is a directory' >&2; exit 1; "
            f"fi && : > {staged}"
        ]
        commands += [
            f"printf %s {encoded[start : start + step]} | base64 -d >> {staged}"
            for start in range(0, len(encoded), step)
        ]
        commands.append(f"mv -f {staged} {target}")
        for command in commands:
            try:
                result = await self._run_bounded(
                    call, command, timeout=self._timeout, max_output_bytes=4096
                )
            except (TimeoutError, SandboxExecOutputLimitExceeded) as unfinished:
                # The command may still be running, so the sandbox went, and with it every
                # file this batch had already put there.
                logger.warning(
                    "%s: shell write of %r did not finish: %s",
                    self._id,
                    path,
                    type(unfinished).__name__,
                )
                raise _BatchLost() from None
            except Exception:
                # The command's end is unknown here too, so the batch ends the same way; the
                # provider's words stay in the log.
                logger.exception("%s: shell write of %r failed", self._id, path)
                raise _BatchLost() from None
            if result.exit_code != 0:
                logger.info(
                    "%s: shell write of %r failed: %s", self._id, path, result.stderr.strip()
                )
                return _shell_error(result.stderr)
        return None

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return _SYNC.run(self.aupload_files(files))

    # --- files out -------------------------------------------------------------------------

    async def _download(
        self, call: _Call, sandbox: Sandbox, path: str, *, room: int
    ) -> FileDownloadResponse:
        """One file, read under the smaller of the per-file cap and ``room``, the batch's rest."""
        per_file = self._spec.files_out.max_bytes_per_file
        cap = min(per_file, room)
        # Which ceiling `cap` is: read off the cap handed down, never off a stat that a
        # growing file has already made stale.
        over_cap = (_OVER_FILE_CAP if cap == per_file else _OVER_TOTAL_CAP).format(
            direction="files_out"
        )
        if not self._inside_base(path):
            if not isinstance(sandbox, BoundedExec):
                return FileDownloadResponse(path=path, error=_NO_SHELL_ROAD)
            return await self._download_via_shell(call, path, cap=cap, over_cap=over_cap)
        try:
            entry = await sandbox.stat_file(path, working_directory=STORAGE_BASE)
        except TimeoutError:
            # Not condemned: a read changes nothing in the sandbox, and the bound is the
            # backend's own, its refusal of an entry it cannot serve (ACAS reads a FIFO the
            # guest planted as a regular file until the bound). The shell road condemns
            # because a command whose end is unknown may still be running.
            logger.warning("%s: stat of %r timed out", self._id, path)
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        except PermissionError as refused:
            logger.info("%s: download of %r refused: %s", self._id, path, refused)
            return FileDownloadResponse(path=path, error=PERMISSION_DENIED)
        except (ValueError, NotADirectoryError) as refused:
            # The plane's own refusals, as on the upload road: a parent that is a file too.
            logger.info("%s: download of %r refused: %s", self._id, path, refused)
            return FileDownloadResponse(path=path, error=INVALID_PATH)
        if entry is None:
            return FileDownloadResponse(path=path, error=FILE_NOT_FOUND)
        if entry.kind is EntryKind.DIRECTORY:
            return FileDownloadResponse(path=path, error=IS_DIRECTORY)
        if entry.kind is not EntryKind.FILE:
            return FileDownloadResponse(path=path, error=INVALID_PATH)
        # `None` fails closed: an unknown size read as free is how a cap stops bounding.
        if entry.size_bytes is None:
            return FileDownloadResponse(path=path, error=_SIZE_UNKNOWN)
        if entry.size_bytes > cap:
            return FileDownloadResponse(path=path, error=over_cap)
        try:
            content = await sandbox.read_file(path, working_directory=STORAGE_BASE, max_bytes=cap)
        except SandboxTransferCapExceeded:
            return FileDownloadResponse(path=path, error=over_cap)
        except TimeoutError:
            # Before the OSError branch: a timeout is one, and the path was not the problem.
            # Not condemned, for the reason given at the stat: the file is failed alone.
            logger.warning("%s: read of %r timed out", self._id, path)
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        except FileNotFoundError:
            return FileDownloadResponse(path=path, error=FILE_NOT_FOUND)
        except IsADirectoryError:
            return FileDownloadResponse(path=path, error=IS_DIRECTORY)
        except PermissionError as refused:
            # Before the OSError branch it is one of: an unreadable file is not a bad path.
            logger.info("%s: download of %r refused: %s", self._id, path, refused)
            return FileDownloadResponse(path=path, error=PERMISSION_DENIED)
        except (ValueError, OSError) as refused:
            logger.info("%s: download of %r refused: %s", self._id, path, refused)
            return FileDownloadResponse(path=path, error=INVALID_PATH)
        if len(content) > cap:
            # The protocol has the caller re-count: a file can grow after the stat, and a
            # backend whose SDK buffers the whole response can only refuse after the fact.
            return FileDownloadResponse(path=path, error=over_cap)
        return FileDownloadResponse(path=path, content=content)

    async def _download_via_shell(
        self, call: _Call, path: str, *, cap: int, over_cap: str
    ) -> FileDownloadResponse:
        """Read outside the base through the shell, under ``cap``; see :meth:`_upload_via_shell`."""
        target = shlex.quote(path)
        # `test -e` is false for a file behind an unsearchable ancestor as for an absent one,
        # so the open's own error, in the shell's words, tells the two apart.
        probe = (
            f"if [ ! -e {target} ]; then ( : < {target} ) 2>&1; echo missing; "
            f"elif [ -d {target} ]; then echo directory; "
            f"elif [ ! -f {target} ]; then echo other; "
            f"elif [ ! -r {target} ]; then echo unreadable; "
            f"else wc -c < {target}; fi"
        )
        try:
            probed = await self._run_bounded(
                call, probe, timeout=self._timeout, max_output_bytes=4096
            )
        except (TimeoutError, SandboxExecOutputLimitExceeded) as unfinished:
            logger.warning(
                "%s: probe of %r did not finish: %s", self._id, path, type(unfinished).__name__
            )
            raise _BatchLost(FileDownloadResponse(path=path, error=_DOWNLOAD_BATCH_LOST)) from None
        except Exception:
            logger.exception("%s: probe of %r failed", self._id, path)
            raise _BatchLost(FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)) from None
        answer = probed.stdout.strip()
        if probed.exit_code != 0 or not answer:
            logger.warning("%s: probe of %r failed: %s", self._id, path, probed.stderr.strip())
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        if answer.endswith("missing"):
            detail = answer.removesuffix("missing")
            if "Permission denied" in detail:
                return FileDownloadResponse(path=path, error=PERMISSION_DENIED)
            if "Not a directory" in detail:
                return FileDownloadResponse(path=path, error=INVALID_PATH)
            return FileDownloadResponse(path=path, error=FILE_NOT_FOUND)
        if answer == "directory":
            return FileDownloadResponse(path=path, error=IS_DIRECTORY)
        if answer == "other":
            return FileDownloadResponse(path=path, error=INVALID_PATH)
        if answer == "unreadable":
            return FileDownloadResponse(path=path, error=PERMISSION_DENIED)
        if not answer.isdigit():
            logger.warning("%s: probe of %r answered %r", self._id, path, answer)
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        if int(answer) > cap:
            return FileDownloadResponse(path=path, error=over_cap)
        try:
            # Base64 is 4/3 of the file plus line breaks; the budget bounds a file that grew.
            read = await self._run_bounded(
                call, f"base64 < {target}", timeout=self._timeout, max_output_bytes=cap * 2 + 4096
            )
        except SandboxExecOutputLimitExceeded:
            # The file outgrew its cap mid-read, and the read may still be running: this file
            # is over the cap, and the rest of the batch is not attempted.
            raise _BatchLost(FileDownloadResponse(path=path, error=over_cap)) from None
        except TimeoutError:
            logger.warning("%s: shell read of %r timed out", self._id, path)
            raise _BatchLost(FileDownloadResponse(path=path, error=_DOWNLOAD_BATCH_LOST)) from None
        except Exception:
            logger.exception("%s: shell read of %r failed", self._id, path)
            raise _BatchLost(FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)) from None
        if read.exit_code != 0:
            logger.warning("%s: shell read of %r failed: %s", self._id, path, read.stderr.strip())
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        try:
            content = base64.b64decode("".join(read.stdout.split()), validate=True)
        except ValueError:
            logger.warning("%s: shell read of %r returned no base64", self._id, path)
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        if len(content) > cap:
            return FileDownloadResponse(path=path, error=over_cap)
        return FileDownloadResponse(path=path, content=content)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        limits = self._spec.files_out
        if len(paths) > limits.max_files:
            refusal = _TOO_MANY_FILES.format(direction="files_out")
            return [FileDownloadResponse(path=path, error=refusal) for path in paths]
        try:
            opened = await self._open(self._timeout)
        except TimeoutError:
            opened = None
        if opened is None:
            return [FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED) for path in paths]
        call, sandbox = opened
        try:
            responses: list[FileDownloadResponse] = []
            room = limits.max_total_bytes
            for path in paths:
                try:
                    response = await self._download(call, sandbox, path, room=room)
                except _BatchLost as lost:
                    responses.append(
                        lost.response or FileDownloadResponse(path=path, error=_DOWNLOAD_BATCH_LOST)
                    )
                    rest = paths[len(responses) :]
                    responses.extend(
                        FileDownloadResponse(path=p, error=_DOWNLOAD_BATCH_LOST) for p in rest
                    )
                    break
                except Exception:
                    logger.exception("%s: download of %r failed", self._id, path)
                    response = FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
                if response.content is not None:
                    room -= len(response.content)
                responses.append(response)
            return responses
        finally:
            await self._leave(call)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return _SYNC.run(self.adownload_files(paths))

    # --- lifecycle -------------------------------------------------------------------------

    async def aclose(self) -> bool:
        """Delete this conversation's sandbox; ``False`` when the delete did not land.

        Admitted exclusively first, so every call over this key, from this adapter or another,
        has left and a delete one of them queued has run. Then deletes the one instance this
        adapter acquired, so a packaged kind serving the same conversation, or another adapter
        over the same key with a different backend or egress, keeps its own; before any
        acquire there is nothing of this adapter's to delete. An instance a sibling's queued
        delete already took is named again here, which the protocol makes a no-op. The
        router's ``dispose_scope`` on the host's conversation-delete path is the backstop for
        a sandbox no ``aclose`` reached.
        """
        owner = uuid.uuid4().hex
        timeout = self._router.reclaim.timeout
        try:
            await self._router.enter_call(
                self._key, self._spec, owner=owner, exclusive=True, timeout=timeout
            )
        except TimeoutError:
            logger.warning("%s: close timed out waiting for the calls in flight", self._id)
            return False
        except Exception:
            logger.exception("%s: close was not admitted", self._id)
            return False
        try:
            instance_id = self._instance_id
            if instance_id is None:
                return True
            disposed = await self._router.dispose_kind(
                self._key, self._spec.kind, instance_id=instance_id, timeout=timeout
            )
            # Forgotten only once the delete landed, so a retry reaches the same instance;
            # kept as is if an acquire replaced it meanwhile.
            if disposed and self._instance_id == instance_id:
                self._instance_id = None
            return disposed
        finally:
            await self._router.release_call(self._key, self._spec.kind, owner=owner)

    def close(self) -> bool:
        """Synchronous :meth:`aclose`."""
        return _SYNC.run(self.aclose())
