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
import hashlib
import json
import logging
import math
import posixpath
import shlex
import threading
import time
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
#: every upload and download path is relative to it. The backend resolves it, whether the
#: spec named a base or left the allocation to the backend.
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
    """One long-lived loop on a thread of its own, for the synchronous surface.

    Deep Agents calls that surface from sync tools, which LangGraph runs on a worker thread
    with no loop, and a caller holding a running loop cannot nest another. One loop for the
    adapter's life rather than one per call, because a backend may cache a client per loop
    (ACAS does) and would otherwise hold one per call until its shutdown. Started on the first
    sync call, stopped by :meth:`stop`.
    """

    _THREAD_NAME = "maf-sandbox-deepagents"

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        with self._guard:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name=self._THREAD_NAME, daemon=True
                )
                self._thread.start()
            loop = self._loop
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    def stop(self) -> None:
        """Stop and close the loop; a no-op from its own thread, which cannot join itself."""
        with self._guard:
            loop, thread = self._loop, self._thread
            if thread is None or thread is threading.current_thread():
                return
            self._loop = self._thread = None
        assert loop is not None
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


def _response(result: ExecResult) -> ExecuteResponse:
    """One combined stream, the way Deep Agents' own backends render it.

    Each ``stderr`` line is prefixed so the model can tell the two apart, and the prefix says
    whose words they are: a producer that took the field is speaking there, not the program.
    """
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        label = "[note]" if result.producer_owns_stderr else "[stderr]"
        parts.extend(f"{label} {line}" for line in result.stderr.strip().splitlines())
    output = "\n".join(parts) if parts else "<no output>"
    return ExecuteResponse(output=output, exit_code=result.exit_code, truncated=False)


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
        if spec.work_dir is None:
            # Deep Agents' file tools take guest paths the model spells out, so the host has to
            # be able to tell it the base; a backend-allocated one is knowable to neither.
            raise ValueError(
                "spec.work_dir must name the storage base: Deep Agents addresses files by "
                "guest path, and a base the backend allocates is one nothing can tell the model"
            )
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
        self._sync = _SyncRunner()
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

    async def _acquire(self) -> Sandbox | None:
        """The conversation's sandbox, or ``None`` with the reason in the log."""
        try:
            return await self._router.acquire(self._key, self._spec)
        except Exception:
            logger.exception("%s: the sandbox could not be acquired", self._id)
            return None

    def _inside_base(self, path: str) -> bool:
        """Whether ``path`` names something under the storage base, the file plane's reach."""
        if not path.startswith("/"):
            return True
        base = PurePosixPath(self._spec.work_dir or "/")
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
            async with asyncio.timeout(bound):
                sandbox = await self._acquire()
        except TimeoutError:
            return ExecuteResponse(output=_TIMED_OUT.format(seconds=bound), exit_code=None)
        if sandbox is None:
            return ExecuteResponse(output=SANDBOX_UNAVAILABLE, exit_code=None)
        if not isinstance(sandbox, BoundedExec):
            logger.error("%s: the backend has no exec_bounded, so no command runs", self._id)
            return ExecuteResponse(output=_UNBOUNDED, exit_code=None)
        remaining = bound - (time.monotonic() - started)
        if remaining <= 0:
            return ExecuteResponse(output=_TIMED_OUT.format(seconds=bound), exit_code=None)
        try:
            result = await sandbox.exec_bounded(
                command,
                working_directory=STORAGE_BASE,
                timeout=remaining,
                max_output_bytes=self._max_output_bytes,
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
        return _response(result)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._sync.run(self.aexecute(command, timeout=timeout))

    # --- files in --------------------------------------------------------------------------

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        limits = self._spec.files_in
        if len(files) > limits.max_files:
            refusal = _TOO_MANY_FILES.format(direction="files_in")
            return [FileUploadResponse(path=path, error=refusal) for path, _ in files]
        sandbox = await self._acquire()
        if sandbox is None:
            return [FileUploadResponse(path=path, error=_UPLOAD_FAILED) for path, _ in files]
        responses: list[FileUploadResponse] = []
        sent = 0
        for path, content in files:
            # Checked before the write it would have prevented, and counted only for what
            # crossed: a refused file leaves the budget where it was.
            error: str | None
            if len(content) > limits.max_bytes_per_file:
                error = _OVER_FILE_CAP.format(direction="files_in")
            elif sent + len(content) > limits.max_total_bytes:
                error = _OVER_TOTAL_CAP.format(direction="files_in")
            elif self._inside_base(path):
                error = await self._upload_via_plane(sandbox, path, content)
            elif isinstance(sandbox, BoundedExec):
                error = await self._upload_via_shell(sandbox, path, content)
            else:
                error = _NO_SHELL_ROAD
            if error is None:
                sent += len(content)
            responses.append(FileUploadResponse(path=path, error=error))
        return responses

    async def _upload_via_plane(self, sandbox: Sandbox, path: str, content: bytes) -> str | None:
        """Write under the base through the file plane; the error code, or ``None``."""
        try:
            await sandbox.write_file(path, content, working_directory=STORAGE_BASE)
        except (ValueError, NotADirectoryError) as refused:
            # The file plane's own refusals — through a link, a parent that is a file — are
            # the guest's shape, safe to name by code.
            logger.info("%s: upload of %r refused: %s", self._id, path, refused)
            return INVALID_PATH
        except Exception:
            logger.exception("%s: upload of %r failed", self._id, path)
            return _UPLOAD_FAILED
        return None

    async def _upload_via_shell(
        self, sandbox: BoundedExec, path: str, content: bytes
    ) -> str | None:
        """Write outside the base through the shell the agent already has.

        Deep Agents writes its offloaded history under ``/conversation_history`` and its
        large-edit temporaries under ``/tmp``, which the file plane, confined to the base,
        cannot reach; ``execute`` can, so this widens nothing. The caps were applied by the
        caller. Base64 in chunks, because a command is one argument to ``sh -c``.
        """
        target = shlex.quote(path)
        parent = shlex.quote(posixpath.dirname(path) or "/")
        encoded = base64.b64encode(content).decode("ascii")
        step = 4 * (_SHELL_CHUNK_BYTES // 3)
        commands = [f"mkdir -p {parent} && : > {target}"]
        commands += [
            f"printf %s {encoded[start : start + step]} | base64 -d >> {target}"
            for start in range(0, len(encoded), step)
        ]
        for command in commands:
            try:
                result = await sandbox.exec_bounded(
                    command,
                    working_directory=STORAGE_BASE,
                    timeout=self._timeout,
                    max_output_bytes=4096,
                )
            except TimeoutError:
                logger.warning("%s: shell write of %r timed out", self._id, path)
                return _UPLOAD_FAILED
            except Exception:
                # Per file, as Deep Agents' contract asks, and the provider's words stay in
                # the log.
                logger.exception("%s: shell write of %r failed", self._id, path)
                return _UPLOAD_FAILED
            if result.exit_code != 0:
                logger.info(
                    "%s: shell write of %r failed: %s", self._id, path, result.stderr.strip()
                )
                return _shell_error(result.stderr)
        return None

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._sync.run(self.aupload_files(files))

    # --- files out -------------------------------------------------------------------------

    async def _download(self, sandbox: Sandbox, path: str, *, room: int) -> FileDownloadResponse:
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
            return await self._download_via_shell(sandbox, path, cap=cap, over_cap=over_cap)
        try:
            entry = await sandbox.stat_file(path, working_directory=STORAGE_BASE)
        except TimeoutError:
            logger.warning("%s: stat of %r timed out", self._id, path)
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        except ValueError as refused:
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
            logger.warning("%s: read of %r timed out", self._id, path)
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        except FileNotFoundError:
            return FileDownloadResponse(path=path, error=FILE_NOT_FOUND)
        except IsADirectoryError:
            return FileDownloadResponse(path=path, error=IS_DIRECTORY)
        except (ValueError, OSError) as refused:
            logger.info("%s: download of %r refused: %s", self._id, path, refused)
            return FileDownloadResponse(path=path, error=INVALID_PATH)
        if len(content) > cap:
            # The protocol has the caller re-count: a file can grow after the stat, and a
            # backend whose SDK buffers the whole response can only refuse after the fact.
            return FileDownloadResponse(path=path, error=over_cap)
        return FileDownloadResponse(path=path, content=content)

    async def _download_via_shell(
        self, sandbox: BoundedExec, path: str, *, cap: int, over_cap: str
    ) -> FileDownloadResponse:
        """Read outside the base through the shell, under ``cap``; see :meth:`_upload_via_shell`."""
        target = shlex.quote(path)
        probe = (
            f"if [ ! -e {target} ]; then echo missing; "
            f"elif [ -d {target} ]; then echo directory; "
            f"elif [ ! -f {target} ]; then echo other; "
            f"elif [ ! -r {target} ]; then echo unreadable; "
            f"else wc -c < {target}; fi"
        )
        probed = await sandbox.exec_bounded(
            probe, working_directory=STORAGE_BASE, timeout=self._timeout, max_output_bytes=4096
        )
        answer = probed.stdout.strip()
        if probed.exit_code != 0 or not answer:
            logger.warning("%s: probe of %r failed: %s", self._id, path, probed.stderr.strip())
            return FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
        if answer == "missing":
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
            read = await sandbox.exec_bounded(
                f"base64 < {target}",
                working_directory=STORAGE_BASE,
                timeout=self._timeout,
                max_output_bytes=cap * 2 + 4096,
            )
        except SandboxExecOutputLimitExceeded:
            return FileDownloadResponse(path=path, error=over_cap)
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
        sandbox = await self._acquire()
        if sandbox is None:
            return [FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED) for path in paths]
        responses: list[FileDownloadResponse] = []
        room = limits.max_total_bytes
        for path in paths:
            try:
                response = await self._download(sandbox, path, room=room)
            except Exception:
                logger.exception("%s: download of %r failed", self._id, path)
                response = FileDownloadResponse(path=path, error=_DOWNLOAD_FAILED)
            if response.content is not None:
                room -= len(response.content)
            responses.append(response)
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._sync.run(self.adownload_files(paths))

    # --- lifecycle -------------------------------------------------------------------------

    async def aclose(self) -> bool:
        """Delete this conversation's sandbox; ``False`` when the delete did not land.

        Deletes this kind alone, so a packaged kind serving the same conversation keeps its own.
        The router's ``dispose_scope`` on the host's conversation-delete path is the backstop for
        a sandbox no ``aclose`` reached.
        """
        disposed = await self._router.dispose_kind(
            self._key, self._spec.kind, timeout=self._router.reclaim.timeout
        )
        self._sync.stop()
        return disposed

    def close(self) -> bool:
        """Synchronous :meth:`aclose`, which also stops the loop the sync surface ran on."""
        disposed = self._sync.run(self.aclose())
        self._sync.stop()
        return disposed
