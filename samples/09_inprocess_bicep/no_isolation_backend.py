"""The no-boundary backend sample 09 wires under the router — :class:`NoIsolationBackend`.

A backend this sample defines that runs workloads on the host with no boundary at all:
``write_file`` writes to a host temp directory and ``exec`` shells out to a real binary on this
machine — the floor of the isolation ladder (:data:`~maf_sandbox.Isolation.NONE`). A backend
is independent of the kinds it serves: this module only knows how to put a file into a host
work directory and run a command there. The workload-specific framing — what this backend is
asked to run, and why — is in ``agent.py``'s docstring and the sample README; this module is
the kind-agnostic implementation.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    DEFAULT_SANDBOX_LIMITS,
    Capability,
    Egress,
    ExecResult,
    Isolation,
    SandboxEntry,
    SandboxKey,
    SandboxLimits,
    SandboxSpec,
)


class NoIsolationSandbox:
    """A :class:`~maf_sandbox.Sandbox` that runs commands on the host — no boundary at all.

    The floor of the isolation ladder: ``write_file`` writes to a host directory and ``exec``
    shells out to a real binary on this machine, and the result is whatever that binary prints.
    There is no container, no VM, no separate filesystem — only the host — so a workload
    written against :class:`~maf_sandbox.Sandbox` runs here unchanged, against a backend with
    no boundary.

    A kind fixes a guest ``work_dir`` (an absolute guest path) and may embed that literal path
    in its command templates. A host backend cannot make an arbitrary absolute guest path real
    on the host, so this sandbox maps the spec's ``work_dir`` to a host temp directory: every
    guest path is rewritten under it, and ``exec`` substitutes it into the command. The mapping
    is honest because ``work_dir`` is known from the spec, not parsed out of an opaque argv —
    the one accommodation a host backend makes, narrower than what the protocol otherwise
    leaves to a kind.

    Only ``write_file`` and ``exec`` are implemented meaningfully. The pull surface
    (``stat_file`` / ``read_file`` / ``list_dir``) raises :exc:`NotImplementedError`: this
    backend declares only :data:`~maf_sandbox.DEFAULT_CAPABILITIES` (``EXEC`` and
    ``FILES_IN``), so the router refuses any workload whose spec requires ``FILES_OUT`` before
    it reaches the backend — the pull surface is never exercised by a workload this backend
    serves.
    """

    def __init__(self, host_root: Path, guest_work_dir: str) -> None:
        self._host_root = host_root
        self._guest_work_dir = guest_work_dir

    def destroy(self) -> None:
        """Remove the host work directory. Best-effort: never raises."""
        shutil.rmtree(self._host_root, ignore_errors=True)

    def _host_path(self, guest_path: str) -> Path:
        """Translate a guest path under ``work_dir`` to a path under the host root.

        ``relative_to`` is lexical and keeps ``..`` segments, so a guest path like
        ``/maf-sandbox/work/../../etc/x`` would join to a path *outside* the host root. The bicep kind
        rejects ``..`` before a path reaches the backend (``_tool.py``), but the docstring says
        paths are rewritten *under* the root, so verify it. The check resolves to collapse ``..``
        and any symlink in the path; the lexical path is still returned, so the host root's string
        form matches what the subprocess prints — the output translation in ``exec`` depends on it.
        """
        rel = PurePosixPath(guest_path).relative_to(self._guest_work_dir)
        host_path = self._host_root.joinpath(*rel.parts)
        if not host_path.resolve().is_relative_to(self._host_root.resolve()):
            raise ValueError(f"guest path {guest_path!r} escapes the work directory")
        return host_path

    async def write_file(self, path: str, content: str | bytes) -> None:
        host_path = self._host_path(path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            host_path.write_text(content, encoding="utf-8")
        else:
            host_path.write_bytes(content)

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        host_cwd = self._host_path(working_directory)
        host_cwd.mkdir(parents=True, exist_ok=True)
        # The protocol's string form is for commands that need shell operators (`2>&1`,
        # `|| true`), so it runs through a shell; a sequence is an argv list and runs without
        # one. Either way, a guest work-directory path embedded in the command must be rewritten
        # to the host root, or the binary runs against a path that does not exist here. A string
        # command is the kind's to build — what it interpolates, and whether that is safe under a
        # shell, is the kind's responsibility, not the backend's.
        if isinstance(command, str):
            cmd: str | list[str] = command.replace(self._guest_work_dir, str(self._host_root))
            shell = True
        else:
            guest = str(self._guest_work_dir)
            host = str(self._host_root)
            cmd = [arg.replace(guest, host) for arg in command]
            shell = False
        # `subprocess.run` blocks, so run it in a worker thread and keep the event loop free
        # for the concurrent tool calls the protocol permits.
        #
        # Known edge, not guarded: on a timeout, `subprocess.run` kills the one process it
        # started — for the shell form that is the shell, and a shell-spawned child (bicep)
        # may outlive it on the host. The argv form has no shell, so it reaps cleanly. This
        # is not guarded in the sample because bicep finishes in well under a second against
        # the workload's 120s per-command timeout, so the orphan path is never walked; a real
        # process-group teardown is disproportionate for a sample and is not unit-pinnable.
        try:
            completed = await asyncio.to_thread(  # noqa: S603 — the string form needs a shell
                subprocess.run,
                cmd,
                shell=shell,
                cwd=str(host_cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(str(exc)) from exc
        # The command was rewritten guest→host, so the binary ran against the host root and prints
        # it back: bicep's SARIF carries `file://` URIs for the host path, and every diagnostic
        # would render with `/tmp/no-isolation-…` where the workload expects to strip the guest
        # `work_dir` and leave `main.bicep`. Reverse the translation in both streams so the guest
        # path the workload strips is the one that appears.
        guest = str(self._guest_work_dir)
        host = str(self._host_root)
        return ExecResult(
            stdout=(completed.stdout or "").replace(host, guest),
            stderr=(completed.stderr or "").replace(host, guest),
            exit_code=completed.returncode,
        )

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        raise NotImplementedError(
            "NoIsolationSandbox does not implement the pull surface (stat_file/read_file/list_dir)"
        )

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        raise NotImplementedError(
            "NoIsolationSandbox does not implement the pull surface (stat_file/read_file/list_dir)"
        )

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        raise NotImplementedError(
            "NoIsolationSandbox does not implement the pull surface (stat_file/read_file/list_dir)"
        )


class NoIsolationBackend:
    """A :class:`~maf_sandbox.SandboxBackend` that runs workloads on the host with no boundary.

    The floor of the isolation ladder — :data:`~maf_sandbox.Isolation.NONE`, "no boundary at
    all: it runs in the host process, with the host's authority". Each ``acquire`` creates a
    host temp directory and hands
    back a :class:`NoIsolationSandbox` that runs commands there; ``dispose_scope`` deletes them.
    Get-or-create is keyed by ``(SandboxKey, spec.kind)`` and guarded by a lock, the way the
    protocol asks.

    ``seed_files`` are written to the work-directory root on acquire — the host's stand-in for
    the files a container image bakes in. Whether a workload needs such a file is the
    workload's concern; the backend simply places whatever the sample passes.

    The egress declaration is a **temporary misuse**: a backend with no boundary honestly
    cannot confine egress, which is :data:`~maf_sandbox.Egress.UNRESTRICTED` — and the router
    refuses ``UNRESTRICTED`` for any workload today. ``CLOSED`` is worn only to pass that
    gate; it is not enforced, and it cannot be. What that unenforced gap means for a given
    workload is the workload's concern, not the backend's, and is argued in the sample README.
    Switch back to ``UNRESTRICTED`` once the core allows it for workloads that don't require
    :data:`~maf_sandbox.Capability.NETWORK` (#265).
    """

    def __init__(
        self,
        *,
        seed_files: Mapping[str, str | bytes] | None = None,
        name: str = "no-isolation",
    ) -> None:
        self._seed_files = dict(seed_files or {})
        self._name = name
        self._sandboxes: dict[tuple[SandboxKey, str], NoIsolationSandbox] = {}
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def isolation(self) -> Isolation:
        return Isolation.NONE

    @property
    def egress(self) -> Egress:
        # Temporary misuse — see the class docstring and #265.
        return Egress.CLOSED

    @property
    def capabilities(self) -> frozenset[Capability]:
        return DEFAULT_CAPABILITIES

    @property
    def limits(self) -> SandboxLimits:
        return DEFAULT_SANDBOX_LIMITS

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> NoIsolationSandbox:
        async with self._lock:
            ident = (key, spec.kind)
            sandbox = self._sandboxes.get(ident)
            if sandbox is None:
                host_root = Path(tempfile.mkdtemp(prefix="no-isolation-"))
                try:
                    for rel, content in self._seed_files.items():
                        # Seed keys are relative to the work root the way a guest
                        # path is, so confine them the same way: an absolute key or a
                        # ``..`` key would land outside the temp directory the
                        # docstring says everything stays under.
                        target = host_root / rel
                        if not target.resolve().is_relative_to(host_root.resolve()):
                            raise ValueError(f"seed file {rel!r} escapes the work directory")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if isinstance(content, str):
                            target.write_text(content, encoding="utf-8")
                        else:
                            target.write_bytes(content)
                except BaseException:
                    # A seeding failure leaves the just-created temp directory
                    # unreachable by disposal (it is not in ``_sandboxes`` yet), so
                    # remove it before re-raising — no leak on a half-built sandbox.
                    shutil.rmtree(host_root, ignore_errors=True)
                    raise
                sandbox = NoIsolationSandbox(host_root, spec.work_dir)
                self._sandboxes[ident] = sandbox
            return sandbox

    async def dispose(self, key: SandboxKey) -> None:
        async with self._lock:
            for ident in [i for i, s in self._sandboxes.items() if i[0] == key]:
                self._sandboxes.pop(ident).destroy()

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        async with self._lock:
            doomed = [
                ident
                for ident in self._sandboxes
                if ident[0].scope == scope and ident[0].thread_id == thread_id
            ]
            for ident in doomed:
                self._sandboxes.pop(ident).destroy()
            return len(doomed)
