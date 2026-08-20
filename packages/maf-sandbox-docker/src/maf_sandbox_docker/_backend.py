"""The docker backend: :class:`~maf_sandbox.SandboxBackend` on plain Docker containers.

Everything provider-specific lives here — the command line, the naming and labelling scheme,
the egress policy, the FILES_OUT pull surface and the label-based purge.  A workload above the
router sees only the :class:`~maf_sandbox.Sandbox` protocol.

Isolation is :data:`~maf_sandbox.Isolation.CONTAINER`: a container shares the host kernel and
sits on the developer's own machine (or a CI runner), below the router's default
:data:`~maf_sandbox.Isolation.MICROVM` floor.  A host that wants this backend opts the floor
down explicitly with ``min_isolation=Isolation.CONTAINER``; with nothing passed, construction
raises :class:`~maf_sandbox.SandboxBackendNotPermitted`.  A Docker Desktop or Colima VM does
not lift the rung — one shared VM kernel serves every container, the same shape as wslc's WSL 2
utility VM, which the ladder classifies at ``container`` — and no configuration ever raises it.

Egress is :data:`~maf_sandbox.Egress.CLOSED` by default — every container is created
``--network none`` — and :data:`~maf_sandbox.Egress.ALLOWLIST` when a proxy image is
configured, enforced by topology exactly as ``maf-sandbox-wslc`` does it.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import posixpath
import re
import tarfile
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol, cast

from maf_sandbox import (
    Capability,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxEntry,
    SandboxKey,
    SandboxLimits,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
)
from maf_sandbox.paths import confine_guest_path, refuse_symlinked_parents

from ._config import DockerSandboxConfig
from ._proxy import build_context

logger = logging.getLogger(__name__)

__all__ = ["BACKEND_NAME", "DockerSandboxBackend"]

#: The name :attr:`DockerSandboxBackend.name` answers to, and the value
#: :class:`~maf_sandbox.SandboxRouter`'s ``selected=`` matches on.
#:
#: Public because a host choosing a backend from its own configuration needs the value before
#: it has a backend to read it off, and building one to learn a constant is a lot of machinery
#: for a fixed string (#411). The property below returns this, so the two cannot disagree.
#:
#: Not a knob. Unlike the in-process backend in ``maf_sandbox.testing``, which takes ``name=``
#: so a host can register several apart, this one is fixed: ``"docker"`` is the word the socket
#: contract is called, and a host reading ``selected="docker"`` should get plain containers.
#:
#: Import it qualified or aliased when more than one backend package is in play. Every backend
#: exports this same symbol, so two `from … import BACKEND_NAME` lines shadow each other and
#: the second wins silently. Either `import maf_sandbox_docker` and reach it as
#: `maf_sandbox_docker.BACKEND_NAME`, or alias at the import:
#: `from maf_sandbox_docker import BACKEND_NAME as DOCKER_BACKEND`.
BACKEND_NAME = "docker"

# Written at create and read back on purge, so the engine is the durable record, not this
# process. The scheme matches wslc's so the label vocabulary is one thing across backends.
_LABEL_SCOPE = "maf-sandbox.scope"
_LABEL_THREAD = "maf-sandbox.thread"
_LABEL_AGENT = "maf-sandbox.agent"
_LABEL_KIND = "maf-sandbox.kind"
_LABEL_PREFIX = "maf-sandbox.label."
#: Marks the egress proxy so a purge can tell it from the sandboxes it counts.
_LABEL_ROLE = "maf-sandbox.role"

_LABEL_VALUE_MAX = 63
_LABEL_VALUE_SAFE = re.compile(r"[A-Za-z0-9._-]+")
_LABEL_VALUE_DIGEST = re.compile(r"sha256-[0-9a-f]{48}")

#: `docker` reports this for a container or object that is not there — the string every
#: teardown treats as a no-op rather than a failure. Matched case-insensitively.
_NO_SUCH = "no such"
#: What `run --name` reports when a *container* name is taken — the create failure that is
#: recoverable by adopting the existing one (`Conflict. The container name … is already in use`).
_ALREADY_IN_USE = "already in use"
#: What `network create` reports for a name already taken (`network with name … already
#: exists`) — a different string from the container conflict above, and adopting the existing
#: network is how warm reuse of an allowlisted sandbox works on the second acquire.
_NETWORK_EXISTS = "already exists"

_PROXY_PORT = 3128
_ALLOW_ENV = "MAF_SANDBOX_ALLOW"
_PROXY_READY_MARKER = "listening"
_PROXY_READY_ATTEMPTS = 20
_PROXY_READY_DELAY_S = 0.25

_NAME_PREFIX = "maf-sandbox-docker-"
_NET_SUFFIX = "-net"
_PROXY_SUFFIX = "-proxy"

#: The tar block size docker's `cp` stream begins with — one header carries name, size, an
#: entry-type flag and a link target before any content byte, which is how this backend stats
#: a path without a stat command.
_TAR_BLOCK = 512

#: This backend's own transfer ceilings, per direction. Named constants, not config: nothing
#: in the tar transport imposes a hard limit, so a ceiling is a policy statement about
#: streaming cost. Set generously above the protocol's spec-side defaults so a spec that says
#: nothing is admitted, and a spec that asks for more is refused with the reason named.
_MIB = 1024 * 1024
_FILES_LIMITS = TransferLimits(
    max_bytes_per_file=64 * _MIB, max_total_bytes=256 * _MIB, max_files=256
)
_LIMITS = SandboxLimits(files_in=_FILES_LIMITS, files_out=_FILES_LIMITS)


def _label_value(raw: str) -> str:
    """A label value that survives ``--label key=value`` and means the same on create and purge.

    Short, plain values pass through so a listing stays readable; anything longer, empty, or
    carrying a character that would split the argument becomes a digest.  Truncation is
    deliberately **not** used: two scopes sharing a prefix would land on the same label, and
    these labels are what :meth:`DockerSandboxBackend.dispose_scope` selects on, so a collision
    would let one conversation's purge delete another's containers.  A value already shaped like
    a digest is digested too, so no caller can hand-make that collision.

    The mapping must stay identical on both sides.  Transform one and not the other and the
    purge quietly selects nothing.
    """
    if (
        len(raw) <= _LABEL_VALUE_MAX
        and _LABEL_VALUE_SAFE.fullmatch(raw)
        and not _LABEL_VALUE_DIGEST.fullmatch(raw)
    ):
        return raw
    return "sha256-" + sha256(raw.encode("utf-8")).hexdigest()[:48]


def _sandbox_labels(key: SandboxKey, spec: SandboxSpec) -> dict[str, str]:
    """The labels a container is created with — the same ones ``dispose_scope`` selects on."""
    return {
        _LABEL_SCOPE: _label_value(key.scope),
        _LABEL_THREAD: _label_value(key.thread_id),
        _LABEL_AGENT: _label_value(key.agent_dir),
        _LABEL_KIND: _label_value(spec.kind),
        **{f"{_LABEL_PREFIX}{k}": _label_value(v) for k, v in spec.labels.items()},
    }


def _container_name(key: SandboxKey, kind: str, egress_id: str = "") -> str:
    """The container name a key and kind map to — derived, so acquire and dispose agree
    without a registry.

    ``kind`` is part of the identity, not decoration: a sandbox carries its spec's image and
    egress, so serving two kinds from one container would run the second workload under the
    first one's network policy.  ``egress_id`` folds the egress configuration in for the same
    reason — a sandbox is reused only by an acquire that wants the *same* egress — and is empty
    for closed egress.  The result matches Docker's name charset
    (``[a-zA-Z0-9][a-zA-Z0-9_.-]*``) with room to spare.

    The fields are length-prefixed before hashing rather than joined by a separator: ``SandboxKey``
    puts no restriction on its strings, so a plain delimiter would let ``scope="a|b", thread="c"``
    and ``scope="a", thread="b|c"`` hash to one name and share a container.  These values are the
    host's request context, not model input, so this is defence in depth rather than a reachable
    exploit — but a length prefix makes the encoding unambiguous for free.
    """
    parts = [key.scope, key.thread_id, key.agent_dir, kind]
    if egress_id:
        parts.append(egress_id)
    digest = sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return f"{_NAME_PREFIX}{digest.hexdigest()[:12]}"


def _network_name(container: str) -> str:
    """The internal network paired with a sandbox container, derived from its name."""
    return f"{container}{_NET_SUFFIX}"


def _proxy_name(container: str) -> str:
    """The egress proxy paired with a sandbox container, derived from its name."""
    return f"{container}{_PROXY_SUFFIX}"


def _stat_from_tar_header(block: bytes, rel_path: str) -> SandboxEntry:
    """Read one ``docker cp`` tar header into a :class:`~maf_sandbox.SandboxEntry`.

    The first 512-byte block of ``docker cp <name>:<path> -`` is the entry's tar header: it
    carries the size, the entry-type flag and the link target, which is everything a stat needs
    and how this backend stats without a stat command.  A symlink maps to
    :data:`~maf_sandbox.EntryKind.SYMLINK` and every other non-regular entry to
    :data:`~maf_sandbox.EntryKind.OTHER`, both with a ``None`` size, so a caller refuses either
    before ever reading a byte.

    A **hard** link stays :data:`~maf_sandbox.EntryKind.OTHER`: it names an inode rather than a
    path, so it is not a way out of the working directory, and it is refused as non-regular
    regardless.
    """
    info = tarfile.TarInfo.frombuf(block, encoding="utf-8", errors="surrogateescape")
    if info.isreg():
        return SandboxEntry(path=rel_path, kind=EntryKind.FILE, size_bytes=info.size)
    if info.isdir():
        return SandboxEntry(path=rel_path, kind=EntryKind.DIRECTORY, size_bytes=None)
    if info.issym():
        return SandboxEntry(path=rel_path, kind=EntryKind.SYMLINK, size_bytes=None)
    return SandboxEntry(path=rel_path, kind=EntryKind.OTHER, size_bytes=None)


@dataclass(frozen=True)
class _DockerResult:
    """What one ``docker`` invocation returned. ``stdout`` is bytes: the read path streams a tar
    through this seam, and decoding it would corrupt every artifact that is not text."""

    returncode: int
    stdout: bytes
    stderr: str


class _DockerRunner(Protocol):
    """The seam every ``docker`` invocation goes through.

    ``read_limit`` bounds how many stdout bytes are read before the child is killed and reaped,
    for the ``docker cp`` read path: a sandbox runs untrusted code, so looking at an output's
    tar header, or enforcing a byte cap on it, must not first buffer the whole file into host
    memory.  ``None`` reads to EOF, which is right for every command whose output is small and
    known (``inspect``, ``ps``, ``logs``).
    """

    async def __call__(
        self,
        *args: str,
        stdin: bytes | None = None,
        timeout: float | None = None,
        read_limit: int | None = None,
    ) -> _DockerResult: ...


class _DockerSandbox:
    """A running container, narrowed to the :class:`~maf_sandbox.Sandbox` protocol."""

    def __init__(self, run: _DockerRunner, name: str, command_timeout: float) -> None:
        self._run = run
        self._name = name
        self._command_timeout = command_timeout

    @property
    def container_name(self) -> str:
        return self._name

    async def write_file(self, path: str, content: str | bytes) -> None:
        """Write ``content`` to ``path`` inside the container, parents included.

        Sent as a one-entry tar on stdin.  A ``cp`` destination must already exist and ``/`` is
        the only path that always does, so the entry name carries the whole path and docker
        creates the missing directories from it.
        """
        data = content.encode("utf-8") if isinstance(content, str) else content
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo(path.lstrip("/"))
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))

        result = await self._run(
            "cp", "-", f"{self._name}:/", stdin=buffer.getvalue(), timeout=self._command_timeout
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker could not write {path}: {result.stderr.strip()}")

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        """Run ``command``, bounded by ``timeout``.

        ``docker exec`` takes argv natively, so a sequence goes through element for element with
        no shell and nothing to quote; a string is a shell command line and runs as ``sh -c``.

        ``timeout`` bounds the host-side call, not the command.  Killing the ``docker exec``
        process does not reach the process it started *inside* the container, and there is no
        per-command handle to kill, so a timed-out call discards the whole sandbox before
        ``TimeoutError`` propagates — a workload reports the hang as a diagnostic, and the next
        acquire pays a fresh create.  A **cancelled** call still reaps the host-side process but
        keeps the sandbox: the in-container command runs on until the sandbox is disposed.
        """
        argv = ["sh", "-c", command] if isinstance(command, str) else list(command)
        try:
            result = await self._run(
                "exec", "-w", working_directory, self._name, *argv, timeout=timeout
            )
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self._run("rm", "-f", self._name, timeout=self._command_timeout)
            raise
        return ExecResult(
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr,
            exit_code=result.returncode,
        )

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        """Describe ``path``, or return ``None`` when nothing is there.

        Reads only the first tar block of ``docker cp <name>:<guest path> -`` and kills the
        transfer: the header carries the size and the entry type, so nothing after it moves,
        and an output too large to serve costs one block rather than its whole self.  A missing
        path is ``None``; a resolution outside ``working_directory`` raises before the
        subprocess runs, and so does a path whose *parents* leave it — no byte of ``/etc``
        crosses when ``out -> /etc`` is statted through, but its type and size do, and that is
        metadata from outside the boundary.

        The **final** component is described rather than refused: a link reported as
        :data:`~maf_sandbox.EntryKind.SYMLINK` is how a caller learns it is one.
        """
        guest = confine_guest_path(path, working_directory)
        await self._refuse_symlinked_parents(guest, working_directory=working_directory)
        return await self._stat_guest(guest, posixpath.normpath(path))

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Delete ``path`` through ``rm``, since the engine has no delete primitive.

        ``rm``'s exit codes are the contract rather than a re-implementation of it: ``-f``
        makes a missing path succeed and refuses a directory without ``-r``. The image
        dependency is the one :attr:`capabilities` already names for ``EXEC``.
        """
        guest = confine_guest_path(path, working_directory)
        await self._refuse_symlinked_parents(guest, working_directory=working_directory)
        if posixpath.normpath(guest) == posixpath.normpath(working_directory):
            raise ValueError(
                f"refusing to remove the working directory itself: {working_directory}"
            )
        removed = await self.exec(
            ["rm", "-rf" if recursive else "-f", "--", guest],
            working_directory=working_directory,
            timeout=self._command_timeout,
        )
        if removed.exit_code != 0:
            raise OSError(
                f"could not remove {path}: rm exited {removed.exit_code}"
                f"{f' — {removed.stderr.strip()}' if removed.stderr else ''}"
            )

    async def _stat_guest(self, guest: str, rel: str) -> SandboxEntry | None:
        """Stat an absolute guest path, with no confinement check of its own.

        Split out because the component walk stats the working directory's own ancestors, which
        by definition sit outside it — confining here would refuse the very check being made.
        """
        result = await self._run(
            "cp", f"{self._name}:{guest}", "-", timeout=self._command_timeout, read_limit=_TAR_BLOCK
        )
        if result.returncode != 0 and not result.stdout:
            if _NO_SUCH in result.stderr.lower() or "could not find" in result.stderr.lower():
                return None
            raise RuntimeError(f"docker could not stat {rel}: {result.stderr.strip()}")
        if len(result.stdout) < _TAR_BLOCK:
            raise RuntimeError(f"docker returned no tar header for {rel}")
        return _stat_from_tar_header(result.stdout[:_TAR_BLOCK], rel)

    async def _refuse_symlinked_parents(self, guest: str, *, working_directory: str) -> None:
        """The protocol's component walk, over this backend's own unconfined stat.

        The :func:`~maf_sandbox.paths.confine_guest_path` paired with it at every call site is
        lexical, so a symlinked *parent* satisfies that one; this is what catches it.  A link
        is only visible when it is the entry being tarred —
        the engine resolves the rest of the path daemon-side — so a symlinked component has to
        be found by walking rather than by judging the path that was asked for.  One header
        read per component.
        """
        await refuse_symlinked_parents(
            lambda directory: self._stat_guest(directory, directory), guest, working_directory
        )

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        """Read the regular file at ``path``, refusing anything over ``max_bytes``.

        The same ``docker cp`` tar stream as :meth:`stat_file`, read only as far as it may
        legitimately go: the header block gives the type and size, and the transfer is bounded
        to header plus ``max_bytes`` before the child is killed, so a file larger than the cap
        is **refused on its header without its body ever being buffered**.  A non-regular entry
        (a symlink tars as a link *entry*, not its target's bytes) is refused on the header
        type, and every parent, from the filesystem root down, is classified first.

        The residual that walk cannot close: a guest that turns a stat-ed component into a link
        between the walk and the read wins, since ``docker cp`` has no no-follow form.
        """
        guest = confine_guest_path(path, working_directory)
        await self._refuse_symlinked_parents(guest, working_directory=working_directory)
        # Header + the most body the cap allows. A larger file is refused from the header alone,
        # so the extra bytes are never read; a file within the cap is fully present in this bound.
        result = await self._run(
            "cp",
            f"{self._name}:{guest}",
            "-",
            timeout=self._command_timeout,
            read_limit=_TAR_BLOCK + max_bytes,
        )
        if result.returncode != 0 and not result.stdout:
            if _NO_SUCH in result.stderr.lower() or "could not find" in result.stderr.lower():
                raise FileNotFoundError(f"no such file: {path!r}")
            raise RuntimeError(f"docker could not read {path}: {result.stderr.strip()}")
        if len(result.stdout) < _TAR_BLOCK:
            raise RuntimeError(f"docker returned no tar header for {path}")
        info = tarfile.TarInfo.frombuf(
            result.stdout[:_TAR_BLOCK], encoding="utf-8", errors="surrogateescape"
        )
        if not info.isreg():
            raise OSError(f"{path!r} is not a regular file and is refused")
        if info.size > max_bytes:
            raise SandboxTransferCapExceeded(
                f"{path!r} is {info.size} bytes and the caller allowed {max_bytes}"
            )
        return result.stdout[_TAR_BLOCK : _TAR_BLOCK + info.size]

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        """Not supported: this backend does not declare :data:`~maf_sandbox.Capability.FILES_LIST`.

        Docker has no engine-level primitive for enumerating a directory, which is exactly why
        that capability is split from ``FILES_OUT``.  The router refuses a spec requiring it
        before a workload runs, so a well-formed caller never reaches here; the raise is the
        honest floor under a caller that skipped the check.
        """
        raise NotImplementedError(
            "the docker backend does not support FILES_LIST: Docker has no engine-level "
            "primitive for enumerating a directory. Declare literal output paths instead."
        )


class DockerSandboxBackend:
    """Hands out container-isolated sandboxes from a Docker-compatible engine."""

    def __init__(self, config: DockerSandboxConfig) -> None:
        self._config = config
        # (scope, thread_id, agent_dir, kind) -> name: a purge fallback for when the listing
        # fails, never the truth. Holds the last name acquired per key and kind.
        self._registry: dict[tuple[str, str, str, str], str] = {}
        # Get-or-create serialised per (running loop, key, kind), for the same reason wslc does
        # it: a create names no container until it returns, so two acquires racing one key would
        # each build a network, a proxy and a sandbox. Per loop because an asyncio.Lock binds to
        # the loop that first waits on it; weak-keyed on the loop so a process that runs a loop
        # per call does not accumulate a lock table for loops long dead.
        self._acquire_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[tuple[str, str, str, str], asyncio.Lock]
        ] = weakref.WeakKeyDictionary()

    @property
    def name(self) -> str:
        return BACKEND_NAME

    @property
    def isolation(self) -> Isolation:
        # A constant, never a function of the config: a container shares the host kernel, and no
        # setting this package has can change that. A hardened runtime (gVisor) would be a
        # different rung, but only with a way to verify it is actually in effect — see the
        # design document's maintainer ruling.
        return Isolation.CONTAINER

    @property
    def egress(self) -> Egress:
        # A capability, not a per-spec fact: the backend can enforce an allowlist iff it has a
        # proxy image to do it with. `acquire` still closes a spec that allows nothing outright.
        if self._config.egress_proxy_image:
            return Egress.ALLOWLIST
        return Egress.CLOSED

    @property
    def capabilities(self) -> frozenset[Capability]:
        # FILES_OUT from day one — the pull surface is native (stat from the first tar header,
        # read from the same stream). Never FILES_LIST: no engine-level enumeration primitive.
        #
        # HOST_TOOLS is the one member with no method behind it, so what it asserts here is
        # narrower than the others and worth stating: `exec` **detaches**. A process started by
        # one call outlives it and is observable from the next, because the container is the
        # sandbox and it stays up between calls — which is what `dispatch_over_exec` is built
        # on, its launcher returning at once and the exit-code file being the run's only
        # witness. `test_docker_e2e.py` measures it rather than assuming it.
        #
        # It is *not* a claim about the image. The shipped launcher wants `sh`, `nohup`,
        # `printf`, `mv`, `mkdir`, `rm` and `kill` — and `setsid` where the image has it — and a
        # kind wants whatever interpreter it names — codeact wants
        # `python3` — none of which this backend chooses, since `spec.image` does. That gap is
        # #111's axis, and it is the same gap `EXEC` already has: a kind execing `python3`
        # against a distroless image fails inside the sandbox today.
        return frozenset(
            {
                Capability.EXEC,
                Capability.FILES_IN,
                Capability.FILES_OUT,
                Capability.FILES_DELETE,
                Capability.HOST_TOOLS,
            }
        )

    @property
    def limits(self) -> SandboxLimits:
        return _LIMITS

    # -- SandboxBackend -----------------------------------------------------------

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> _DockerSandbox:
        """Return a running container for ``key``, reusing a warm one when there is one.

        The egress scaffolding is (re-)ensured on every acquire, not only on create: a proxy a
        host reboot stopped, or one a crashed setup left half-connected, is rebuilt here rather
        than leaving a sandbox that declares an allowlist and enforces nothing. Reused, restarted
        and created are logged at INFO.
        """
        egress_id = self._egress_id(spec)
        name = _container_name(key, spec.kind, egress_id)
        async with self._acquire_lock(key, spec.kind):
            running = await self._is_running(name)
            stopped = not running and await self._exists(name)
            if egress_id:
                await self._ensure_egress(name, key, spec, fresh=not running and not stopped)

            if running:
                verb = "reused"
            elif stopped and await self._restart(name):
                verb = "restarted"
            else:
                image = await self._create_workload(name, key, spec, allowlisting=bool(egress_id))
                logger.info(
                    "sandbox created: container=%s kind=%s image=%s thread=%s agent=%s",
                    name,
                    spec.kind,
                    image,
                    key.thread_id,
                    key.agent_dir,
                )
                verb = ""
            if verb:
                logger.info(
                    "sandbox %s: container=%s kind=%s thread=%s agent=%s",
                    verb,
                    name,
                    spec.kind,
                    key.thread_id,
                    key.agent_dir,
                )

            self._registry[(key.scope, key.thread_id, key.agent_dir, spec.kind)] = name
            return _DockerSandbox(self._docker, name, self._config.command_timeout_seconds)

    async def dispose(self, key: SandboxKey) -> None:
        """Delete every container for ``key`` — every kind, closed or allowlisted — with
        proxies and networks.

        By label, so it reaches a sandbox created under an egress configuration this backend no
        longer runs; the registry name is the fallback for when the listing itself fails. Never
        raises.
        """
        prefix = (key.scope, key.thread_id, key.agent_dir)
        mine = [k for k in list(self._registry) if k[:3] == prefix]
        remembered = [self._registry.pop(k) for k in mine]
        await self._purge(
            [
                (_LABEL_SCOPE, key.scope),
                (_LABEL_THREAD, key.thread_id),
                (_LABEL_AGENT, key.agent_dir),
            ],
            fallback=remembered,
            thread_id=key.thread_id,
        )

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        """Delete every container labelled ``(scope, thread_id)``; returns how many sandboxes.

        The labels are the source of truth, because a conversation delete has to reach
        containers this process never created. The registry is the fallback for when the listing
        fails, and its entries are dropped either way.
        """
        mine = [k for k in list(self._registry) if k[0] == scope and k[1] == thread_id]
        remembered = [self._registry.pop(k) for k in mine]
        return await self._purge(
            [(_LABEL_SCOPE, scope), (_LABEL_THREAD, thread_id)],
            fallback=remembered,
            thread_id=thread_id,
        )

    async def _purge(
        self, label_filters: list[tuple[str, str]], fallback: list[str], thread_id: str
    ) -> int:
        """Remove the containers a label query returns, plus their proxies and networks.

        A proxy carries its sandbox's labels, so it is listed and removed alongside it, but it
        is not a sandbox and is not counted. Its network is removed after it. The ``fallback``
        names cover the case the listing failed.

        Every workload's derived proxy and network are swept regardless of this backend's
        current egress config — not gated on it — because a sandbox created while an allowlist
        was configured must be fully reclaimable through a backend now configured closed; the
        removal helpers treat a missing proxy or network as a no-op, so the extra attempts on a
        genuinely closed sandbox cost nothing but a call.
        """
        listed = await self._list_names_by_labels(label_filters)
        listed_set = set(listed)
        stranded = [n for n in fallback if n not in listed_set]
        names = [*listed, *stranded]

        count = 0
        for target in names:
            if await self._remove(target) and not target.endswith(_PROXY_SUFFIX):
                logger.info("sandbox released: container=%s thread=%s (purge)", target, thread_id)
                count += 1

        networks = {
            _network_name(n.removesuffix(_PROXY_SUFFIX))
            for n in listed
            if n.endswith(_PROXY_SUFFIX)
        }
        for workload in (n for n in names if not n.endswith(_PROXY_SUFFIX)):
            if _proxy_name(workload) not in listed_set:
                await self._remove(_proxy_name(workload))
            networks.add(_network_name(workload))
        for net in networks:
            await self._remove_network(net)
        return count

    # -- internals ----------------------------------------------------------------

    async def _docker(
        self,
        *args: str,
        stdin: bytes | None = None,
        timeout: float | None = None,
        read_limit: int | None = None,
    ) -> _DockerResult:
        """Run one ``docker`` command — the single seam every invocation goes through.

        Bytes out, so the read path's tar stream survives intact.  Any abnormal end to the wait
        — a timeout, a cancelled caller — kills the subprocess and reaps it before the exception
        propagates.  A missing or stopped daemon surfaces here as its own ``docker`` error on
        the returned ``stderr``; callers name it rather than letting a bare non-zero propagate.

        With ``read_limit`` set, stdout is read only up to that many bytes and the child is then
        killed and reaped rather than drained: the ``docker cp`` read path must never buffer a
        whole untrusted output just to read its tar header or to enforce a byte cap, so an
        oversized file costs ``read_limit`` bytes and no more.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                self._config.docker_path,
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except NotImplementedError as exc:
            # A selector loop on Windows raises this with no message at all — the same trap wslc
            # guards, and a property of spawning subprocesses from Windows rather than of docker.
            raise ValueError(
                "the docker backend needs an event loop that can spawn subprocesses — on Windows "
                "that is asyncio's default Proactor loop"
            ) from exc
        except FileNotFoundError as exc:
            # The client binary itself is not on PATH — a configuration error, named as one.
            raise RuntimeError(
                f"the docker client {self._config.docker_path!r} was not found on PATH; set "
                "DockerSandboxConfig.docker_path to the client binary (or 'podman')"
            ) from exc
        if read_limit is not None:
            return await self._read_bounded(process, read_limit, timeout)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
        except BaseException:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        return _DockerResult(
            process.returncode or 0,
            stdout,
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    async def _read_bounded(
        process: asyncio.subprocess.Process, read_limit: int, timeout: float | None
    ) -> _DockerResult:
        """Read at most ``read_limit`` stdout bytes, then kill and reap — the cp read path.

        stderr is read only after the child is killed, so a pipe that fills cannot deadlock the
        bounded stdout read against it — and it is short in every case that matters (``docker
        cp`` writes its error there and nothing else).  A returncode of ``None`` after the kill
        is normal for a file larger than ``read_limit`` and means nothing to the caller, which
        decides on the tar header it now holds.
        """
        assert process.stdout is not None and process.stderr is not None
        # Bound to locals so the narrowing survives into the closure below.
        out_stream = process.stdout
        err_stream = process.stderr

        async def _pull_head() -> bytes:
            chunks: list[bytes] = []
            got = 0
            while got < read_limit:
                chunk = await out_stream.read(min(65536, read_limit - got))
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            return b"".join(chunks)

        try:
            stdout = await asyncio.wait_for(_pull_head(), timeout=timeout)
        except BaseException:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        with contextlib.suppress(Exception):
            process.kill()
        try:
            stderr = await asyncio.wait_for(err_stream.read(), timeout=timeout)
        except Exception:
            # Best-effort diagnostics only. `Exception`, not `BaseException`: this branch
            # swallows rather than re-raising (unlike the stdout read above, which cleans up and
            # re-raises), so it must let `CancelledError` and `KeyboardInterrupt` through.
            stderr = b""
        with contextlib.suppress(Exception):
            await process.wait()
        return _DockerResult(
            process.returncode or 0, stdout, stderr.decode("utf-8", errors="replace")
        )

    async def _is_running(self, name: str) -> bool:
        """Whether ``name`` is a container that exists and is running.

        ``docker inspect`` answers both existence and state in one call — it errors for a
        container that is not there, and prints the running boolean for one that is — which is
        the cleaner primitive wslc's substring-matching listing did not have.
        """
        result = await self._docker(
            "inspect",
            "-f",
            "{{.State.Running}}",
            name,
            timeout=self._config.command_timeout_seconds,
        )
        return result.returncode == 0 and result.stdout.decode("utf-8", "replace").strip() == "true"

    async def _exists(self, name: str) -> bool:
        """Whether a container named ``name`` exists in any state."""
        result = await self._docker(
            "inspect", "-f", "{{.State.Status}}", name, timeout=self._config.command_timeout_seconds
        )
        return result.returncode == 0

    async def _restart(self, name: str) -> bool:
        """Start an existing container, removing it if it will not start.

        The name is what a replacement ``run`` needs back; leaving a broken container under it
        would fail every acquire from here on.
        """
        result = await self._docker("start", name, timeout=self._config.command_timeout_seconds)
        if result.returncode == 0:
            return True
        logger.info(
            "container %s did not start (%s); creating a replacement", name, result.stderr.strip()
        )
        await self._remove(name)
        return False

    def _egress_id(self, spec: SandboxSpec) -> str:
        """The egress folded into a sandbox's identity — empty when it has no allowlist to keep.

        A proxy image plus a non-empty allowlist means allowlisted egress; either missing means
        closed, and closed is the empty string so a closed sandbox keeps its historical name.

        "Missing" is truthiness, matching what `egress` declares off the same field. It used to
        be `is None` here, which made `egress_proxy_image=""` declare CLOSED and then try to
        `docker run` the empty string as an image — the one value where the declaration and the
        behaviour disagreed, and the one an unset environment variable produces (#407).
        """
        if not self._config.egress_proxy_image or not spec.egress_allow:
            return ""
        return "allow:" + ",".join(sorted(spec.egress_allow))

    def _acquire_lock(self, key: SandboxKey, kind: str) -> asyncio.Lock:
        """The get-or-create lock for one key and kind on the running loop (see ``__init__``)."""
        per_loop = self._acquire_locks.setdefault(asyncio.get_running_loop(), {})
        registry_key = (key.scope, key.thread_id, key.agent_dir, kind)
        lock = per_loop.get(registry_key)
        if lock is None:
            lock = per_loop[registry_key] = asyncio.Lock()
        return lock

    async def _ensure_image(self, image: str) -> None:
        """Pull ``image`` if it is not already present, under the pull timeout.

        ``docker run`` would pull an absent image implicitly, folding a multi-hundred-megabyte
        network transfer into ``command_timeout_seconds``; probing first with ``image inspect``
        keeps a warm create off the registry entirely and gives a cold pull its own, larger
        bound.
        """
        present = await self._docker(
            "image", "inspect", image, timeout=self._config.command_timeout_seconds
        )
        if present.returncode == 0:
            return
        pulled = await self._docker(
            "image", "pull", image, timeout=self._config.image_pull_timeout_seconds
        )
        if pulled.returncode != 0:
            raise RuntimeError(f"docker could not pull image {image}: {pulled.stderr.strip()}")

    async def _create_workload(
        self, name: str, key: SandboxKey, spec: SandboxSpec, *, allowlisting: bool
    ) -> str:
        """Create and start the workload container; returns the image it ran.

        The network and proxy already exist by now (``_ensure_egress`` ran first), so this only
        places the workload: on ``--network none`` when closed, or on the internal network with
        the proxy in its environment when allowlisting.  Hardening flags go on unconditionally
        (``--security-opt no-new-privileges``, ``--pids-limit``) or from config (``--cap-drop
        ALL``, ``--memory``, ``--cpus``); no bind mount, no host path and no socket ever cross.
        """
        image = spec.image_id or spec.image
        if not image:
            raise ValueError(
                "No sandbox image is configured: the spec names neither image nor image_id."
            )
        await self._ensure_image(image)

        args = ["run", "-d", "--name", name]
        args += [
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self._config.pids_limit),
        ]
        if self._config.cap_drop_all:
            args += ["--cap-drop", "ALL"]
        if self._config.memory is not None:
            args += ["--memory", self._config.memory]
        if self._config.cpus is not None:
            args += ["--cpus", str(self._config.cpus)]
        if allowlisting:
            proxy_url = f"http://{_proxy_name(name)}:{_PROXY_PORT}"
            args += ["--network", _network_name(name)]
            args += ["-e", f"HTTPS_PROXY={proxy_url}", "-e", f"HTTP_PROXY={proxy_url}"]
        else:
            args += ["--network", "none"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["--label", f"{label}={value}"]
        args += [image, "sleep", "infinity"]

        result = await self._docker(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0:
            if _ALREADY_IN_USE in result.stderr.lower() and await self._adopt(name):
                logger.info("container %s already existed; adopted it instead of creating", name)
                return image
            raise RuntimeError(f"docker could not create container {name}: {result.stderr.strip()}")
        return image

    async def _ensure_egress(
        self, name: str, key: SandboxKey, spec: SandboxSpec, *, fresh: bool
    ) -> None:
        """Build (or repair) the internal network and filtering proxy for an allowlisted sandbox.

        ``fresh`` says no workload is attached yet, so if the proxy cannot be brought up the
        network this just created is ours to reclaim rather than leak; once a warm workload is
        on it, the network stays and the proxy failure surfaces to the caller instead.  The
        proxy is removed *before* the network: a proxy left attached to the network — a
        ``network connect`` or readiness failure leaves exactly that — would make the network
        removal fail on "has active endpoints", stranding both.
        """
        net = _network_name(name)
        await self._ensure_network(net, key, spec)
        try:
            await self._ensure_proxy(name, key, spec)
        except BaseException:
            if fresh:
                await self._remove(_proxy_name(name))
                await self._remove_network(net)
            raise

    async def _ensure_network(self, net: str, key: SandboxKey, spec: SandboxSpec) -> None:
        """Create the sandbox's internal network, adopting one already there.

        ``network create`` reports an existing name as "already exists" (not the container
        conflict's "already in use"), and adopting it rather than failing is what lets a second
        acquire of an allowlisted sandbox reuse its network.
        """
        args = ["network", "create", "--internal"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["--label", f"{label}={value}"]
        args.append(net)
        result = await self._docker(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0 and _NETWORK_EXISTS not in result.stderr.lower():
            raise RuntimeError(f"docker could not create network {net}: {result.stderr.strip()}")

    async def _ensure_proxy(self, name: str, key: SandboxKey, spec: SandboxSpec) -> None:
        """Put a fresh filtering proxy on the sandbox's network, dual-homed and confirmed listening.

        Recreated every acquire, not adopted: a fresh proxy always carries the current spec's
        allowlist, always gets its outbound leg connected, and its log holds only this run's
        readiness line — so a stale, half-connected or wrong-allowlist proxy is never mistaken
        for a working one.
        """
        # Sound rather than nearly sound: this is only reached through a truthy `_egress_id`,
        # which reads the same field the same way (#407).
        proxy_image = cast("str", self._config.egress_proxy_image)
        proxy = _proxy_name(name)
        await self._remove(proxy)

        args = ["run", "-d", "--name", proxy, "--network", _network_name(name)]
        args += ["-e", f"{_ALLOW_ENV}={','.join(spec.egress_allow)}"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["--label", f"{label}={value}"]
        args += ["--label", f"{_LABEL_ROLE}=proxy", proxy_image]

        result = await self._docker(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker could not start the egress proxy {proxy}: {result.stderr.strip()} — "
                f"if the image is missing, build it: docker build -t "
                f"{proxy_image} {build_context()}"
            )

        connect = await self._docker(
            "network",
            "connect",
            self._config.outbound_network,
            proxy,
            timeout=self._config.command_timeout_seconds,
        )
        if connect.returncode != 0:
            # A proxy without its egress leg would turn ALLOWLIST into CLOSED silently.
            raise RuntimeError(
                f"docker could not give the egress proxy {proxy} its outbound leg on network "
                f"{self._config.outbound_network!r}: {connect.stderr.strip()}"
            )
        await self._await_listening(proxy)

    async def _await_listening(self, proxy: str) -> None:
        """Wait for the proxy's listening line; fail the acquire if it never comes.

        A proxy that has not bound its port yet would let the workload's first request through
        to nothing and read as a network error. Rather than hand back a sandbox whose egress is
        not actually up, the acquire fails here and the caller can retry — the network is
        reclaimed on the way out when this was a fresh create.
        """
        for _ in range(_PROXY_READY_ATTEMPTS):
            result = await self._docker("logs", proxy, timeout=self._config.command_timeout_seconds)
            if result.returncode == 0 and _PROXY_READY_MARKER in result.stdout.decode(
                "utf-8", "replace"
            ):
                return
            await asyncio.sleep(_PROXY_READY_DELAY_S)
        raise RuntimeError(f"egress proxy {proxy} never reported listening")

    async def _adopt(self, name: str) -> bool:
        """Whether an existing ``name`` is running, or could be started — the reuse path again.

        The check that sent ``acquire`` down the create branch can be out of date by the time
        ``run`` executes: two acquires for one key race, or a transient failure hid a container
        that is right there.  Without this the name stays taken and every acquire for that key
        fails from then on.
        """
        if await self._is_running(name):
            return True
        return await self._exists(name) and await self._restart(name)

    async def _remove(self, target: str) -> bool:
        """Force-remove ``target``. Returns whether it removed one; never raises."""
        try:
            result = await self._docker(
                "rm", "-f", target, timeout=self._config.command_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.warning("docker backend: failed to remove container %s: %s", target, exc)
            return False
        if result.returncode == 0:
            return True
        if _NO_SUCH not in result.stderr.lower():
            logger.warning(
                "docker backend: failed to remove container %s: %s", target, result.stderr.strip()
            )
        return False

    async def _list_names_by_labels(self, label_filters: list[tuple[str, str]]) -> list[str]:
        """Container names matching every ``(label, value)`` filter, read from docker. Never raises.

        ``docker ps -a --filter label=k=v --format '{{.Names}}'`` selects by label and returns
        the names directly, newline-delimited.  ``_label_value`` on both sides, always: these
        have to be the same strings the create wrote, or the query matches nothing and every
        container for the deleted conversation keeps running — silently, since "found none" and
        "there were none" are one result.
        """
        args = ["ps", "-a", "--format", "{{.Names}}"]
        for label, value in label_filters:
            args += ["--filter", f"label={label}={_label_value(value)}"]
        try:
            result = await self._docker(*args, timeout=self._config.command_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - purge must never fail
            logger.warning("docker backend: could not list containers to purge: %s", exc)
            return []
        if result.returncode != 0:
            logger.warning(
                "docker backend: could not list containers to purge: %s", result.stderr.strip()
            )
            return []
        return [line for line in result.stdout.decode("utf-8", "replace").splitlines() if line]

    async def _remove_network(self, net: str) -> bool:
        """Force-remove a network. Returns whether it removed one; never raises.

        A network that was never there is a no-op, not a failure — an allowlisting backend's
        purge tries a workload's network whether or not that workload turns out to have had one.
        """
        try:
            result = await self._docker(
                "network", "rm", net, timeout=self._config.command_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.warning("docker backend: failed to remove network %s: %s", net, exc)
            return False
        if result.returncode == 0:
            return True
        # Keep the network-only wording local so container failures remain visible.
        if _NO_SUCH not in result.stderr.lower() and "not found" not in result.stderr.lower():
            logger.warning(
                "docker backend: failed to remove network %s: %s", net, result.stderr.strip()
            )
        return False


# The package's strict pyright pass type-checks this assignment. ``runtime_checkable`` tests
# member *presence* only, so a narrowed signature or a missing method passes `isinstance` and
# fails here instead — in the package where the divergence would be introduced.
if TYPE_CHECKING:
    _: tuple[SandboxBackend, type[Sandbox]] = (
        DockerSandboxBackend(DockerSandboxConfig()),
        _DockerSandbox,
    )
