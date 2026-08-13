"""The no-boundary backend sample 09 wires under the router — :class:`NoIsolationBackend`.

A backend this sample defines that runs workloads on the host with no boundary at all:
``write_file`` writes to a host temp directory and ``exec`` shells out to a real binary on this
machine — the floor of the isolation ladder (:data:`~maf_sandbox.Isolation.PROCESS`), carrying
the real bicep compiler rather than a scripted fake. The full story — the guest-path mapping,
the ``bicepconfig.json`` seeding, and the egress temporary-misuse — is in ``agent.py``'s
docstring and the sample README; this module is the implementation.
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
    There is no container, no VM, no separate filesystem — only the host. That is the point of
    this sample: the same bicep workload that runs in a container (samples 01/02/05) runs here
    unchanged, against a backend with no boundary.

    The bicep kind fixes a guest ``work_dir`` (``/acas/work``) and embeds that literal path in
    its command templates. A host backend cannot make ``/acas/work`` a real rootless path, so
    this sandbox maps the spec's ``work_dir`` to a host temp directory: every guest path is
    rewritten under it, and ``exec`` substitutes it into the command. The mapping is honest
    because ``work_dir`` is known from the spec, not parsed out of an opaque argv — the one
    accommodation a host backend makes, narrower than what the protocol otherwise leaves to a
    kind.

    Only ``write_file`` and ``exec`` are exercised by the bicep workload. The pull surface
    (``stat_file`` / ``read_file`` / ``list_dir``) raises: the workload's spec requires only
    ``EXEC`` and ``FILES_IN``, never ``FILES_OUT``, so a backend that does not implement the
    pull surface is refused no workload it is asked to serve.
    """

    def __init__(self, host_root: Path, guest_work_dir: str) -> None:
        self._host_root = host_root
        self._guest_work_dir = guest_work_dir

    def destroy(self) -> None:
        """Remove the host work directory. Best-effort: never raises."""
        shutil.rmtree(self._host_root, ignore_errors=True)

    def _host_path(self, guest_path: str) -> Path:
        """Translate a guest path under ``work_dir`` to a path under the host root."""
        rel = PurePosixPath(guest_path).relative_to(self._guest_work_dir)
        return self._host_root.joinpath(*rel.parts)

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
        # A string command carries shell features (`2>&1`, `|| true`) the bicep templates need,
        # so it runs through a shell; a sequence is an argv list and runs without one. The only
        # interpolated content in the bicep command is the file path, which the workload
        # validated to [A-Za-z0-9._/-] before it reached here — not agent-controlled.
        if isinstance(command, str):
            cmd: str | list[str] = command.replace(
                self._guest_work_dir, str(self._host_root)
            )
            shell = True
        else:
            cmd = list(command)
            shell = False
        try:
            completed = subprocess.run(  # noqa: S603 — shell only for the string form, above
                cmd,
                shell=shell,
                cwd=str(host_cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(str(exc)) from exc
        return ExecResult(
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            exit_code=completed.returncode,
        )

    async def stat_file(
        self, path: str, *, working_directory: str
    ) -> SandboxEntry | None:
        raise NotImplementedError(
            "NoIsolationSandbox does not implement the pull surface; the bicep workload "
            "uses only write_file and exec"
        )

    async def read_file(
        self, path: str, *, working_directory: str, max_bytes: int
    ) -> bytes:
        raise NotImplementedError(
            "NoIsolationSandbox does not implement the pull surface; the bicep workload "
            "uses only write_file and exec"
        )

    async def list_dir(
        self, path: str, *, working_directory: str
    ) -> tuple[SandboxEntry, ...]:
        raise NotImplementedError(
            "NoIsolationSandbox does not implement the pull surface; the bicep workload "
            "uses only write_file and exec"
        )


class NoIsolationBackend:
    """A :class:`~maf_sandbox.SandboxBackend` that runs workloads on the host with no boundary.

    The floor of the isolation ladder — :data:`~maf_sandbox.Isolation.PROCESS`, "same process
    as the host, no boundary at all" — carrying a real compiler rather than a scripted fake.
    Each ``acquire`` creates a host temp directory and hands back a :class:`NoIsolationSandbox`
    that runs commands there; ``dispose_scope`` deletes them. Get-or-create is keyed by
    ``(SandboxKey, spec.kind)`` and guarded by a lock, the way the protocol asks.

    ``seed_files`` are written to the work-directory root on acquire — the host's stand-in for
    the files a container image bakes in. The bicep workload expects ``bicepconfig.json`` at
    the work-directory root (bicep finds it by walking up from the source file), so the sample
    seeds it here to lint under the same rule set as samples 01/02/05.

    The egress declaration is a **temporary misuse**: a backend with no boundary honestly
    cannot confine egress, which is :data:`~maf_sandbox.Egress.UNRESTRICTED` — and the router
    refuses ``UNRESTRICTED`` for any workload today. ``CLOSED`` is worn only to pass the
    router's gate; it is not enforced, and it is safe here only because ``main.bicep``
    references no modules and makes no egress calls. Switch back to ``UNRESTRICTED`` once the
    core allows it for workloads that don't require :data:`~maf_sandbox.Capability.NETWORK`
    (#265).
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
        return Isolation.PROCESS

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
                for rel, content in self._seed_files.items():
                    target = host_root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(content, str):
                        target.write_text(content, encoding="utf-8")
                    else:
                        target.write_bytes(content)
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
