"""The adapter: a ``maf_sandbox`` router behind Deep Agents' ``BaseSandbox``.

Deep Agents hands an agent one ``execute`` tool over a sandbox object the host constructs, and
derives its file tools from ``execute`` and ``upload_files``. This module implements that
object over a :class:`~maf_sandbox.SandboxRouter`: the router still refuses a backend below
the host's isolation floor or one that cannot enforce the spec's egress mode, and the sandbox
is still keyed from the host's request context and purged with the conversation.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
from collections.abc import Coroutine
from typing import Any

from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    IS_DIRECTORY,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from maf_sandbox import (
    DEFAULT_TRANSFER_LIMITS,
    Capability,
    Egress,
    EgressRule,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    Sandbox,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
)

__all__ = [
    "DEEPAGENTS_KIND",
    "DEFAULT_EXEC_TIMEOUT_SECONDS",
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
#: ``write_file``, files pulled out for ``download_files``. The derived tools run on
#: ``execute`` alone.
REQUIRED_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}
)

DEFAULT_EXEC_TIMEOUT_SECONDS = 120.0

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

_TIMED_OUT = "Error: the command did not finish within {seconds:g} seconds and was stopped."
#: Distinct from :data:`SANDBOX_UNAVAILABLE`: the command may have run, and the result is what
#: did not come back.
_EXEC_FAILED = "Error: the sandbox did not return the command's result; see the host log."
_UPLOAD_FAILED = "upload failed; see the host log"
_DOWNLOAD_FAILED = "download failed; see the host log"
_SIZE_UNKNOWN = "the sandbox could not report the file's size"
_TOO_MANY_FILES = "the batch has more files than {direction}.max_files allows"
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

    ``work_dir`` names the base for an image with a fixed layout; ``None`` lets the backend
    allocate one.
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


def _run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run ``coroutine`` to completion from synchronous code.

    Deep Agents calls the synchronous surface from sync tools, which LangGraph runs on a worker
    thread with no loop; a caller that does hold a running loop gets a fresh one on a thread of
    its own, since ``asyncio.run`` refuses to nest.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="maf-sandbox-deepagents"
    ) as worker:
        return worker.submit(asyncio.run, coroutine).result()


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


class MafSandbox(BaseSandbox):
    """A :class:`~maf_sandbox.SandboxRouter` as a Deep Agents sandbox.

    One instance serves one conversation: ``key`` is the host's scope, thread and agent
    directory, read from the host's request context and never from the model. The sandbox is
    acquired on first use — get-or-create, so it stays warm across turns — and lives until the
    host disposes it: :meth:`aclose` here, or the router's ``dispose_scope`` on the host's own
    conversation-delete path, which is what every other sandbox in the suite answers to.

    Construction refuses what the router refuses — a backend below the isolation floor, one that
    cannot enforce the spec's egress mode, a missing capability — so a misconfigured host fails
    before an agent is built, not on its first command.

    Deep Agents' derived file tools (``ls``, ``read_file``, ``write_file``, ``edit_file``,
    ``glob``, ``grep``) run ``python3`` inside the guest; on an image without it only ``execute``
    and this class's own upload and download work. The image is the host's to choose.
    """

    def __init__(
        self,
        router: SandboxRouter,
        key: SandboxKey,
        spec: SandboxSpec,
        *,
        exec_timeout_seconds: float = DEFAULT_EXEC_TIMEOUT_SECONDS,
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
        if exec_timeout_seconds <= 0:
            raise ValueError("exec_timeout_seconds must be positive")
        router.ensure_can_serve(spec)
        self._router = router
        self._key = key
        self._spec = spec
        self._timeout = float(exec_timeout_seconds)
        backend = router.backend_for(spec)
        served_by = "" if backend is None else backend.name
        digest = hashlib.sha256(
            "\0".join((served_by, key.scope, key.thread_id, key.agent_dir, spec.kind)).encode()
        ).hexdigest()
        # The id names the sandbox, as a provider's own id would: two adapters over one key,
        # kind and backend reach one sandbox through the router's get-or-create, and say so.
        # Opaque rather than the key spelled out, because Deep Agents may render it to the
        # model and a scope is often a tenant or a user.
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

    # --- execute ---------------------------------------------------------------------------

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            return ExecuteResponse(output="Error: Command must be a non-empty string.", exit_code=1)
        bound = self._timeout if timeout is None else float(timeout)
        if bound <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        sandbox = await self._acquire()
        if sandbox is None:
            return ExecuteResponse(output=SANDBOX_UNAVAILABLE, exit_code=None)
        try:
            result = await sandbox.exec(command, working_directory=STORAGE_BASE, timeout=bound)
        except TimeoutError:
            return ExecuteResponse(output=_TIMED_OUT.format(seconds=bound), exit_code=None)
        except Exception:
            logger.exception("%s: the command's result could not be read", self._id)
            return ExecuteResponse(output=_EXEC_FAILED, exit_code=None)
        return _response(result)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return _run_sync(self.aexecute(command, timeout=timeout))

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
            if len(content) > limits.max_bytes_per_file:
                refusal = _OVER_FILE_CAP.format(direction="files_in")
                responses.append(FileUploadResponse(path=path, error=refusal))
                continue
            if sent + len(content) > limits.max_total_bytes:
                refusal = _OVER_TOTAL_CAP.format(direction="files_in")
                responses.append(FileUploadResponse(path=path, error=refusal))
                continue
            try:
                await sandbox.write_file(path, content, working_directory=STORAGE_BASE)
            except (ValueError, NotADirectoryError) as refused:
                # The file plane's own refusals — outside work_dir, through a link, a parent
                # that is a file — are the guest's shape, safe to name by code.
                logger.info("%s: upload of %r refused: %s", self._id, path, refused)
                responses.append(FileUploadResponse(path=path, error=INVALID_PATH))
            except Exception:
                logger.exception("%s: upload of %r failed", self._id, path)
                responses.append(FileUploadResponse(path=path, error=_UPLOAD_FAILED))
            else:
                sent += len(content)
                responses.append(FileUploadResponse(path=path))
        return responses

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return _run_sync(self.aupload_files(files))

    # --- files out -------------------------------------------------------------------------

    async def _download(self, sandbox: Sandbox, path: str, *, room: int) -> FileDownloadResponse:
        """One file, read under the smaller of the per-file cap and ``room``, the batch's rest."""
        cap = min(self._spec.files_out.max_bytes_per_file, room)
        try:
            entry = await sandbox.stat_file(path, working_directory=STORAGE_BASE)
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
            return FileDownloadResponse(path=path, error=self._over_cap(entry.size_bytes))
        try:
            content = await sandbox.read_file(path, working_directory=STORAGE_BASE, max_bytes=cap)
        except SandboxTransferCapExceeded:
            return FileDownloadResponse(path=path, error=self._over_cap(entry.size_bytes))
        except FileNotFoundError:
            return FileDownloadResponse(path=path, error=FILE_NOT_FOUND)
        except IsADirectoryError:
            return FileDownloadResponse(path=path, error=IS_DIRECTORY)
        except (ValueError, OSError) as refused:
            logger.info("%s: download of %r refused: %s", self._id, path, refused)
            return FileDownloadResponse(path=path, error=INVALID_PATH)
        return FileDownloadResponse(path=path, content=content)

    def _over_cap(self, size: int) -> str:
        """Which ceiling a file of ``size`` bytes broke: its own, or what the batch had left."""
        direction = "files_out"
        if size > self._spec.files_out.max_bytes_per_file:
            return _OVER_FILE_CAP.format(direction=direction)
        return _OVER_TOTAL_CAP.format(direction=direction)

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
        return _run_sync(self.adownload_files(paths))

    # --- lifecycle -------------------------------------------------------------------------

    async def aclose(self) -> bool:
        """Delete this conversation's sandbox; ``False`` when the delete did not land.

        Deletes this kind alone, so a packaged kind serving the same conversation keeps its own.
        The router's ``dispose_scope`` on the host's conversation-delete path is the backstop for
        a sandbox no ``aclose`` reached.
        """
        return await self._router.dispose_kind(
            self._key, self._spec.kind, timeout=self._router.reclaim.timeout
        )

    def close(self) -> bool:
        """Synchronous :meth:`aclose`."""
        return _run_sync(self.aclose())
