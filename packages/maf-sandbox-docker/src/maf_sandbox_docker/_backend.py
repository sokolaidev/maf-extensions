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
import time
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol, cast

from maf_sandbox import (
    Capability,
    DisposalFailure,
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
    ScopePurge,
    TransferLimits,
    fold_disposal_failures,
)
from maf_sandbox.paths import (
    confine_guest_path,
    confine_guest_write_path,
    guest_directory_chain,
    refuse_symlinked_parents,
)

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

#: How much of `/etc/passwd` the identity read will take off the wire: the header plus a
#: body big enough for any real passwd file, so a host that answers keeps the whole file
#: and a pathological one cannot stream unbounded.
_PASSWD_READ_LIMIT = _TAR_BLOCK + 64 * 1024

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


def _image_reference(spec: SandboxSpec) -> str:
    """The reference this backend will actually run, or ``""`` when the spec names none.

    ``image_id`` wins over ``image`` and can be the only one set, so anything keyed on which
    image a container holds keys on this.
    """
    return spec.image_id or spec.image or ""


def _stat_from_tar_header(info: tarfile.TarInfo, rel_path: str) -> SandboxEntry:
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
    if info.isreg():
        return SandboxEntry(path=rel_path, kind=EntryKind.FILE, size_bytes=info.size)
    if info.isdir():
        return SandboxEntry(path=rel_path, kind=EntryKind.DIRECTORY, size_bytes=None)
    if info.issym():
        return SandboxEntry(path=rel_path, kind=EntryKind.SYMLINK, size_bytes=None)
    return SandboxEntry(path=rel_path, kind=EntryKind.OTHER, size_bytes=None)


def _no_component_was_the_guests(walked: Mapping[str, tuple[int, int]]) -> bool:
    """Could the guest program have swapped any directory on this path?

    Not where every one is root's and writable by nobody else.  Empty is ``True``: a walk that
    reached nothing found nothing writable.  See ``docs/sandbox/backends/docker.md``.
    """
    return all(uid == 0 and not mode & 0o022 for uid, mode in walked.values())


@dataclass(frozen=True)
class _ContainerFacts:
    """What a running container says about itself, as against what this backend asked for.

    A container name carries neither the image nor the hardening, so a reused container can
    predate a change to either and this backend's config is not evidence about it.
    """

    #: Every directory above ``work_dir`` is root's and writable by nobody else.
    host_owned_ancestors: bool
    #: Runs with ``--cap-drop ALL``, so root holds no ``CAP_DAC_OVERRIDE``.
    capabilities_dropped: bool
    #: The default user UID, or 0 when unknown.
    guest_uid: int = 0
    #: The default user GID, or 0 when unknown.
    guest_gid: int = 0


@dataclass(frozen=True)
class _DockerResult:
    """What one ``docker`` invocation returned. ``stdout`` is bytes: the read path streams a tar
    through this seam, and decoding it would corrupt every artifact that is not text."""

    returncode: int
    stdout: bytes
    stderr: str


@dataclass(frozen=True)
class _Removal:
    """What one force-remove did: whether a container went away, and why one did not.

    Both, because a container that was already gone is neither — nothing was removed, and
    nothing is wrong.
    """

    removed: bool
    failure: DisposalFailure | None = None


@dataclass(frozen=True)
class _Sweep:
    """What one label sweep did: sandboxes removed, and the workload containers still there.

    ``undeleted`` maps a container name to why its removal failed, so a caller can report the
    reason and remember the name to try again.  ``unlisted`` is the one thing with no name
    behind it: the label query itself failed, so the sweep cannot claim to have covered
    containers another replica created.
    """

    count: int
    undeleted: Mapping[str, DisposalFailure] = field(default_factory=dict[str, DisposalFailure])
    unlisted: DisposalFailure | None = None

    @property
    def reason(self) -> DisposalFailure | None:
        """Everything this sweep could not do, as one sentence, or ``None`` when it is clean."""
        return fold_disposal_failures(
            [*([self.unlisted] if self.unlisted is not None else []), *self.undeleted.values()]
        )


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

    def __init__(
        self,
        run: _DockerRunner,
        name: str,
        command_timeout: float,
        cap_drop_all: bool = False,
        reclaim_as_root: bool = True,
        guest_uid: int = 0,
        guest_gid: int = 0,
    ) -> None:
        self._run = run
        self._name = name
        self._command_timeout = command_timeout
        # Both read from the container at acquire, not taken from this backend's config.
        self._reclaim_as_root = reclaim_as_root
        self._cap_drop_all = cap_drop_all
        self._guest_uid = guest_uid
        self._guest_gid = guest_gid

    @property
    def container_name(self) -> str:
        return self._name

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        """Write ``content`` to ``path`` inside the container, parents included.

        Sent as a tar on stdin, the entries carrying the whole path, because a ``cp``
        destination must already exist and ``/`` is the only one that always does.

        Two constraints decide which directories get an entry.  Docker creates an
        **implicit** intermediate as ``root`` whatever the file entry's ownership says, so
        a missing directory has to travel as its own explicit entry under the container's
        user; and an entry naming a directory that already exists re-stamps its mode, so an
        existing one must not.  Which is which comes from the confinement walk this call
        already paid for, rather than a second stat.

        The entries stop at ``working_directory``: an absent ancestor above it is docker's
        to create as root, since a guest-owned entry there would be a redirect the reach
        rule never cleared, and ``/`` is the destination and needs none.
        """
        walked: dict[str, tuple[int, int]] = {}
        guest = await confine_guest_write_path(
            lambda p: self._stat_guest(p, p, walked), path, working_directory
        )
        data = content.encode("utf-8") if isinstance(content, str) else content
        guest_work_dir = posixpath.normpath(working_directory)
        guest_leaf_dir = posixpath.dirname(guest)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            missing = [
                guest_dir
                for guest_dir in guest_directory_chain(guest_leaf_dir, guest_work_dir)
                if guest_dir not in walked
                and guest_dir != "/"
                and (
                    guest_dir == guest_work_dir
                    or guest_dir.startswith(
                        guest_work_dir if guest_work_dir == "/" else guest_work_dir + "/"
                    )
                )
            ]
            for guest_directory in missing:
                entry = tarfile.TarInfo(guest_directory.lstrip("/") + "/")
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o755
                entry.uid = self._guest_uid
                entry.gid = self._guest_gid
                archive.addfile(entry)
            entry = tarfile.TarInfo(guest.lstrip("/"))
            entry.size = len(data)
            entry.mode = 0o644
            entry.uid = self._guest_uid
            entry.gid = self._guest_gid
            archive.addfile(entry, io.BytesIO(data))

        result = await self._run(
            "cp", "-", f"{self._name}:/", stdin=buffer.getvalue(), timeout=self._command_timeout
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker could not write {path}: {result.stderr.strip()}")

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        """Run ``command`` as the image's user, bounded by ``timeout``.

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
        return await self._exec(argv, working_directory=working_directory, timeout=timeout)

    async def _exec(
        self,
        argv: Sequence[str],
        *,
        working_directory: str,
        timeout: float,
        as_root: bool = False,
    ) -> ExecResult:
        """One ``docker exec``, as the image's user or as ``--user 0``.

        :meth:`remove` and :meth:`reclaim` ask for root; :meth:`exec` and :meth:`run_code` are
        the guest program's own and name no user.  See ``docs/sandbox/backends/docker.md``.
        """
        privilege = ("--user", "0") if as_root else ()
        try:
            result = await self._run(
                "exec", *privilege, "-w", working_directory, self._name, *argv, timeout=timeout
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

    async def ancestors_are_the_hosts(self, work_dir: str) -> bool:
        """Is every directory *above* ``work_dir`` one the guest program cannot write?

        One tar header per component.  Raises whatever the stat raises; the caller decides what
        an unreadable component means.
        """
        walked: dict[str, tuple[int, int]] = {}
        for directory in guest_directory_chain(posixpath.dirname(work_dir), "/"):
            await self._stat_guest(directory, directory, walked)
        return _no_component_was_the_guests(walked)

    async def _removal(
        self,
        argv: Sequence[str],
        *,
        working_directory: str,
        timeout: float,
        raise_authority: bool,
    ) -> ExecResult:
        """Run a removal, as root where ``raise_authority`` says the reach rule allows it.

        A root refusal is retried as the image's user where this container holds no
        ``CAP_DAC_OVERRIDE``, and both attempts' messages reach the caller.  ``timeout`` is one
        deadline across both, not one each.  See ``docs/sandbox/backends/docker.md``.
        """
        if not raise_authority:
            return await self._exec(argv, working_directory=working_directory, timeout=timeout)
        started = time.monotonic()
        removed = await self._exec(
            argv, working_directory=working_directory, timeout=timeout, as_root=True
        )
        if removed.exit_code == 0 or not self._cap_drop_all:
            return removed
        left = timeout - (time.monotonic() - started)
        if left <= 0:
            return removed
        logger.debug(
            "docker: %s refused a removal as root (exit %d: %s), retrying as the image's user "
            "with %.1fs left",
            self._name,
            removed.exit_code,
            removed.stderr.strip() or "no output",
            left,
        )
        retried = await self._exec(argv, working_directory=working_directory, timeout=left)
        if retried.exit_code == 0 or not removed.stderr.strip():
            return retried
        return replace(
            retried, stderr=f"{retried.stderr.strip()} (as root: {removed.stderr.strip()})"
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

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        """Not supported: this backend declares no :data:`~maf_sandbox.Capability.RUN_CODE`.

        Not for want of an interpreter — the image may well carry one — but because *which*
        runtime an image carries is a property of the image, and this backend is handed image
        references it does not parse. Declaring the capability would be a claim about someone
        else's artefact. A workload that wants a runtime by name invokes it through
        :meth:`exec` and owns that assumption itself.
        """
        raise NotImplementedError(
            "the docker backend does not support RUN_CODE: evaluating code without a shell "
            "means knowing which runtime the guest carries, and this backend resolves an "
            "image reference without looking inside it. Run the interpreter through exec, or "
            "register a backend that declares RUN_CODE."
        )

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Delete ``path`` through ``rm``, since the engine has no delete primitive.

        ``rm``'s exit codes are the contract rather than a re-implementation of it: ``-f``
        makes a missing path succeed and refuses a directory without ``-r``. The image
        dependency is the one :attr:`capabilities` already names for ``EXEC``.

        Runs as root only where no component of the path was the guest's, which the walk this
        already owes answers.  See ``docs/sandbox/backends/docker.md``.
        """
        guest = confine_guest_path(path, working_directory)
        walked: dict[str, tuple[int, int]] = {}
        await self._refuse_symlinked_parents(
            guest, working_directory=working_directory, walked=walked
        )
        if posixpath.normpath(guest) == posixpath.normpath(working_directory):
            raise ValueError(
                f"refusing to remove the working directory itself: {working_directory}"
            )
        removed = await self._removal(
            ["rm", "-rf" if recursive else "-f", "--", guest],
            working_directory=working_directory,
            timeout=self._command_timeout,
            raise_authority=_no_component_was_the_guests(walked),
        )
        if removed.exit_code != 0:
            raise OSError(
                f"could not remove {path}: rm exited {removed.exit_code}"
                f"{f' — {removed.stderr.strip()}' if removed.stderr else ''}"
            )

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Remove ``directory`` with ``rm -rf``, through :meth:`_removal`.

        Runs from ``/`` because ``working_directory`` may not exist, and takes no confinement
        check: the caller made ``directory``, and :func:`~maf_sandbox.reclaim_guest_path` is
        where that policy lives.  The floor below re-refuses a subset of it, because this
        command runs from ``/`` and can carry root's authority.

        Why root is allowed without a walk, and which half of the argument is checked at
        acquire rather than asserted: ``docs/sandbox/backends/docker.md``.
        """
        del working_directory
        if not directory.startswith("/"):
            raise ValueError(f"refusing to reclaim a path that is not absolute: {directory}")
        if len([part for part in posixpath.normpath(directory).split("/") if part]) < 2:
            raise ValueError(f"refusing to reclaim recursively that close to the root: {directory}")
        removed = await self._removal(
            ["rm", "-rf", "--", directory],
            working_directory="/",
            timeout=timeout,
            raise_authority=self._reclaim_as_root,
        )
        if removed.exit_code != 0:
            raise OSError(
                f"could not reclaim {directory}: rm exited {removed.exit_code}"
                f"{f' — {removed.stderr.strip()}' if removed.stderr else ''}"
            )

    async def _stat_guest(
        self, guest: str, rel: str, walked: dict[str, tuple[int, int]] | None = None
    ) -> SandboxEntry | None:
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
        info = tarfile.TarInfo.frombuf(
            result.stdout[:_TAR_BLOCK], encoding="utf-8", errors="surrogateescape"
        )
        if walked is not None:
            # The same header answers ownership, so a walk that wants both parses it once.
            walked[guest] = (info.uid, info.mode)
        return _stat_from_tar_header(info, rel)

    async def _refuse_symlinked_parents(
        self,
        guest: str,
        *,
        working_directory: str,
        walked: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        """The protocol's component walk, over this backend's own unconfined stat.

        The :func:`~maf_sandbox.paths.confine_guest_path` paired with it at every call site is
        lexical, so a symlinked *parent* satisfies that one; this is what catches it.  A link
        is only visible when it is the entry being tarred —
        the engine resolves the rest of the path daemon-side — so a symlinked component has to
        be found by walking rather than by judging the path that was asked for.  One header
        read per component.
        """
        await refuse_symlinked_parents(
            lambda directory: self._stat_guest(directory, directory, walked),
            guest,
            working_directory,
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
        #: Workload containers a removal could not take away, by key prefix. Retry
        #: bookkeeping only: it does **not** keep one from being served, since the name comes
        #: from the key and `acquire` asks the engine. Refusing to serve is the router's
        #: ledger. A `dispose` clears an entry once the removal lands; a scope purge never
        #: does, because a name is not a generation and it cannot tell the two apart.
        self._undeleted: dict[tuple[str, str, str], set[str]] = {}
        # Get-or-create serialised per (running loop, key, kind), for the same reason wslc does
        # it: a create names no container until it returns, so two acquires racing one key would
        # each build a network, a proxy and a sandbox. Per loop because an asyncio.Lock binds to
        # the loop that first waits on it; weak-keyed on the loop so a process that runs a loop
        # per call does not accumulate a lock table for loops long dead.
        self._acquire_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[tuple[str, str, str, str], asyncio.Lock]
        ] = weakref.WeakKeyDictionary()
        # (container, image, work_dir) -> what the container itself says. Keyed on the image
        # because a container name is not, so one name can come back carrying a different one.
        self._facts: dict[tuple[str, str, str], _ContainerFacts] = {}

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
    def egress_modes(self) -> frozenset[Egress]:
        # With a proxy image it can allowlist named hosts or deny all; without one it can only
        # run `--network none`. Never UNRESTRICTED: a container backend always cuts or proxies,
        # so it cannot offer a workload that asked to run open.
        if self._config.egress_proxy_image:
            return frozenset({Egress.ALLOWLIST, Egress.CLOSED})
        return frozenset({Egress.CLOSED})

    @property
    def capabilities(self) -> frozenset[Capability]:
        # FILES_OUT from day one — the pull surface is native (stat from the first tar header,
        # read from the same stream). Never FILES_LIST: no engine-level enumeration primitive.
        #
        # HOST_TOOLS is the one member with no method behind it, so what it asserts here is
        # narrower than the others and worth stating: `exec` **detaches**. A process started by
        # one call outlives it and is observable from the next, because the container is the
        # sandbox and it stays up between calls — which is what `host_tool_calls_over_exec` is
        # built on, its launcher returning at once and the exit-code file being the run's only
        # witness. `test_docker_e2e.py` measures it rather than assuming it.
        #
        # It is *not* a claim about the image. The shipped launcher wants `sh`, `nohup`,
        # `printf`, `mv`, `mkdir`, `rm` and `kill` — and `setsid` where the image has it —
        # and a run directory it can write, which the write plane stamps with the container
        # user on an image that identifies one; and a kind wants whatever interpreter it
        # names — codeact wants
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
                # A create means this name is about to be a different container, whether the
                # last one was removed through this backend or vanished behind its back.
                self._forget_facts(name)
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
            facts = await self._container_facts(name, spec)
            return _DockerSandbox(
                self._docker,
                name,
                self._config.command_timeout_seconds,
                facts.capabilities_dropped,
                facts.host_owned_ancestors,
                facts.guest_uid,
                facts.guest_gid,
            )

    async def _capabilities_dropped(self, name: str) -> bool:
        """Does this container run without ``CAP_DAC_OVERRIDE``?

        Read from the container, never from :attr:`DockerSandboxConfig.cap_drop_all`, which
        describes what this backend would create rather than what it reused.  Unknown counts as
        dropped.
        """
        result = await self._docker(
            "inspect",
            "-f",
            "{{.HostConfig.CapDrop}}",
            name,
            timeout=self._config.command_timeout_seconds,
        )
        if result.returncode != 0:
            return True
        return "ALL" in result.stdout.decode("utf-8", errors="replace").upper()

    async def _guest_identity(self, name: str, probe: _DockerSandbox) -> tuple[int, int]:
        """Read the container's default user's uid and gid.

        An unset user is root by definition, and an explicit ``uid:gid`` pair — both
        numeric — is taken as-is; anything else is resolved against the container's own
        account files, read over the same ``docker cp`` pull surface every other fact
        uses, because the primary gid comes from ``/etc/passwd`` (or a named group from
        ``/etc/group``) and is not the uid's to guess — a bare ``0`` no more implies gid
        ``0`` than ``10001`` implies ``10001``.  ``id`` inside the container answers when
        the account files cannot be read.  When nothing answers, the write falls back to
        root's ``0:0`` for an image that positively identifies nothing, rather than a guess
        that could stamp a stranger's ownership.

        Only the ``id`` step's ``TimeoutError`` propagates, because only it runs a guest
        command and ``_exec`` removes the container on its way out: that is a dying sandbox
        rather than an unreadable identity, and caching fallback facts for it would serve
        ``acquire`` a container that no longer exists.  A host-side read that times out has
        removed nothing, so it falls back like any other unreadable answer.
        """
        uid: int | None = None
        gid: int | None = None
        group_name: str | None = None
        named_a_user = False
        try:
            result = await self._docker(
                "inspect",
                "-f",
                "{{.Config.User}}",
                name,
                timeout=self._config.command_timeout_seconds,
            )
            if result.returncode != 0:
                # A daemon that will not answer and an image that names no user both leave
                # an empty string, and they are opposite cases: the second is root by
                # definition and returns silently, the first is what the warning below is
                # for.  Branching here is what keeps them apart.
                raise RuntimeError(result.stderr.strip() or f"exit {result.returncode}")
            raw = result.stdout.decode("utf-8", errors="replace").strip()
            if not raw or raw == "0:0":
                return 0, 0
            named_a_user = True
            user_spec, _, group_spec = raw.partition(":")
            # An empty user half is docker's own shorthand for root: `:20001` runs as
            # `0:20001` and a bare `:` as `0:0` (both measured).  Reading it here is what
            # keeps a gid the field already stated from being discarded on an image with no
            # `id` to answer the uid.
            uid = 0 if not user_spec else (int(user_spec) if user_spec.isdigit() else None)
            gid = int(group_spec) if group_spec.isdigit() else None
            group_name = group_spec if group_spec and not group_spec.isdigit() else None
            # `/etc/passwd` answers a uid, and a gid only when the group half is not
            # named; a bare uid beside a named group (`10001:devs`) is entirely
            # `/etc/group`'s to answer, so do not pull passwd for it.
            if uid is None or (gid is None and group_name is None):
                passwd = await self._passwd_entry(name)
                if passwd is not None:
                    for line in passwd.splitlines():
                        fields = line.strip().split(":")
                        if len(fields) < 4 or not fields[2].isdigit() or not fields[3].isdigit():
                            continue
                        if uid is None and fields[0] == user_spec:
                            uid = int(fields[2])
                        # The named-group case must not inherit the passwd line's gid:
                        # `app:devs` means gid comes from `/etc/group`, not app's own.
                        if (
                            gid is None
                            and group_name is None
                            and uid is not None
                            and fields[2] == str(uid)
                        ):
                            gid = int(fields[3])
                        if uid is not None and gid is not None:
                            break
            if gid is None and group_name is not None:
                groups = await self._group_entry(name)
                gid = groups.get(group_name)
        except Exception as unreadable:  # noqa: BLE001 — an acquire must not fail over this
            logger.debug("docker: could not read %s's guest identity (%s)", name, unreadable)
        # Outside that `except` deliberately; the docstring says why.
        if named_a_user and (uid is None or gid is None):
            u_res, g_res = await self._effective_ids(
                probe, want_uid=uid is None, want_gid=gid is None
            )
            uid = uid if uid is not None else u_res
            gid = gid if gid is not None else g_res
        if uid is not None and gid is not None:
            return uid, gid
        if uid is not None:
            # A positively-known uid with no gid answer: the runtime picks 0 for a uid with
            # no passwd entry, so that is the honest remainder.
            return uid, 0
        logger.warning(
            "docker: %s's user could not be resolved, so its files stay root-owned and the "
            "guest cannot empty its own call directory; give the image a numeric uid:gid in "
            "Config.User, or a readable /etc/passwd, or an `id` it can run",
            name,
        )
        return 0, 0

    async def _effective_ids(
        self, probe: _DockerSandbox, *, want_uid: bool, want_gid: bool
    ) -> tuple[int | None, int | None]:
        """What ``id`` says, asked only for the halves the account files left open.

        Each half is asked for and kept on its own.  A spec like ``app:20001`` names the gid
        already, so asking for it spends a guest command on an answer that is in hand — and
        an ``id -g`` that hangs would take the container with it, over a uid the other half
        was about to resolve.
        """
        return (
            await self._one_effective_id(probe, "-u") if want_uid else None,
            await self._one_effective_id(probe, "-g") if want_gid else None,
        )

    async def _one_effective_id(self, probe: _DockerSandbox, flag: str) -> int | None:
        """One half of ``id``, or ``None`` when the guest will not answer it."""
        result = await probe.exec(
            ["id", flag],
            working_directory="/",
            timeout=self._config.command_timeout_seconds,
        )
        answer = result.stdout.strip()
        return int(answer) if result.exit_code == 0 and answer.isdigit() else None

    async def _passwd_entry(self, name: str) -> str | None:
        """The container's ``/etc/passwd``, over the pull surface, or ``None`` when unreadable.

        Read host-side so a named user resolves on an image carrying no ``id`` (or no shell
        to run it through) — the same ``docker cp`` stat_file uses, minus the header cap.
        """
        try:
            result = await self._docker(
                "cp",
                f"{name}:/etc/passwd",
                "-",
                timeout=self._config.command_timeout_seconds,
                read_limit=_PASSWD_READ_LIMIT,
            )
        except Exception as unreadable:  # noqa: BLE001 — an acquire must not fail over this
            logger.debug("docker: could not read %s's /etc/passwd (%s)", name, unreadable)
            return None
        # `and`, not `or`, and the same rule `_stat_guest` reads by: a bounded read kills the
        # child once the cap is reached, so a nonzero code with bytes in hand means the stream
        # was longer than the cap — not that the read failed.  The header and body length
        # checks below decide whether what arrived is complete.
        if result.returncode != 0 and not result.stdout:
            return None
        if len(result.stdout) < _TAR_BLOCK:
            return None
        try:
            info = tarfile.TarInfo.frombuf(
                result.stdout[:_TAR_BLOCK], encoding="utf-8", errors="surrogateescape"
            )
        except (tarfile.TarError, EOFError, ValueError):
            return None
        if not info.isreg():
            return None
        body = result.stdout[_TAR_BLOCK : _TAR_BLOCK + info.size]
        if len(body) < info.size:
            return None
        return body.decode("utf-8", errors="replace")

    async def _group_entry(self, name: str) -> dict[str, int]:
        """The container's ``/etc/group`` as ``{group name: gid}``, or ``{}`` when unreadable.

        The named-group half of `Config.User` (`app:devs`) resolves here, by the same
        ``docker cp`` pull the passwd half uses.
        """
        try:
            result = await self._docker(
                "cp",
                f"{name}:/etc/group",
                "-",
                timeout=self._config.command_timeout_seconds,
                read_limit=_PASSWD_READ_LIMIT,
            )
        except Exception as unreadable:  # noqa: BLE001 — an acquire must not fail over this
            logger.debug("docker: could not read %s's /etc/group (%s)", name, unreadable)
            return {}
        if result.returncode != 0 and not result.stdout:
            return {}
        if len(result.stdout) < _TAR_BLOCK:
            return {}
        try:
            info = tarfile.TarInfo.frombuf(
                result.stdout[:_TAR_BLOCK], encoding="utf-8", errors="surrogateescape"
            )
        except (tarfile.TarError, EOFError, ValueError):
            return {}
        if not info.isreg():
            return {}
        body = result.stdout[_TAR_BLOCK : _TAR_BLOCK + info.size]
        if len(body) < info.size:
            return {}
        groups: dict[str, int] = {}
        for line in body.decode("utf-8", errors="replace").splitlines():
            fields = line.strip().split(":")
            if len(fields) >= 3 and fields[2].isdigit():
                groups[fields[0]] = int(fields[2])
        return groups

    async def _container_facts(self, name: str, spec: SandboxSpec) -> _ContainerFacts:
        """Read what ``name`` says about itself, once per container.

        Here rather than in :meth:`_DockerSandbox.reclaim` because the ancestor chain is fixed
        before any guest runs: one answer per container, not one walk per call.  **Fails
        closed** — an unreadable component leaves removals at the guest's authority.  See
        ``docs/sandbox/backends/docker.md``.
        """
        key = (name, _image_reference(spec), spec.work_dir)
        cached = self._facts.get(key)
        if cached is not None:
            return cached
        probe = _DockerSandbox(self._docker, name, self._config.command_timeout_seconds)
        try:
            answer = await probe.ancestors_are_the_hosts(spec.work_dir)
        except Exception as unreadable:  # noqa: BLE001 — an acquire must not fail over this
            logger.debug("docker: could not read %s's work dir ancestors (%s)", name, unreadable)
            answer = False
        if not answer:
            logger.info(
                "docker: %s has a directory above %s the guest may write, so removals run as "
                "the guest rather than as root",
                name,
                spec.work_dir,
            )
        try:
            guest_uid, guest_gid = await self._guest_identity(name, probe)
        except TimeoutError:
            raise
        except Exception as unreadable:  # noqa: BLE001 — an acquire must not fail over this
            logger.debug("docker: could not read %s's guest identity (%s)", name, unreadable)
            guest_uid, guest_gid = 0, 0
        facts = _ContainerFacts(
            host_owned_ancestors=answer,
            capabilities_dropped=await self._capabilities_dropped(name),
            guest_uid=guest_uid,
            guest_gid=guest_gid,
        )
        self._facts[key] = facts
        return facts

    def _forget_facts(self, container: str) -> None:
        """Drop what ``container`` said about itself, whatever key it was read under.

        A name is not a container.  Every entry for one has to go the moment this backend
        knows the name will mean a different container, or a removal decides its principal
        from a container that no longer exists.
        """
        for cached in [key for key in self._facts if key[0] == container]:
            del self._facts[cached]

    async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
        """Delete every container for ``key`` — every kind, closed or allowlisted — with
        proxies and networks.

        By label, so it reaches a sandbox created under an egress configuration this backend no
        longer runs; the registry name is the fallback for when the listing itself fails. Never
        raises: a ``docker rm`` that failed comes back as the reason, so the router can refuse
        a key whose data is still sitting in a container.
        """
        prefix = (key.scope, key.thread_id, key.agent_dir)
        mine = [k for k in list(self._registry) if k[:3] == prefix]
        remembered = [self._registry.pop(k) for k in mine]
        candidates = list(dict.fromkeys([*remembered, *sorted(self._undeleted.get(prefix, ()))]))
        if candidates:
            # Before the first await: the registry no longer holds these, so a retry finds
            # them only here. Merged, not assigned — teardown for one key is not serialized.
            self._undeleted[prefix] = self._undeleted.get(prefix, set()) | set(candidates)
        swept = await self._purge(
            [
                (_LABEL_SCOPE, key.scope),
                (_LABEL_THREAD, key.thread_id),
                (_LABEL_AGENT, key.agent_dir),
            ],
            fallback=candidates,
            thread_id=key.thread_id,
        )
        # Only on a normal return, so a cancelled sweep keeps the whole set; over-retaining is
        # the safe direction, since a container already gone drops out next attempt. Read from
        # the live map, not the snapshot, with no await between read and write. A name does not
        # identify a generation, so a stale sweep can still subtract a newer record: #685.
        still = set(swept.undeleted)
        left = (self._undeleted.get(prefix, set()) | still) - (set(candidates) - still)
        if left:
            self._undeleted[prefix] = left
        else:
            self._undeleted.pop(prefix, None)
        reported = swept.reason
        if reported is not None:
            return reported
        if left:
            # A disposal still in flight wrote these ahead of its own await. `None` would
            # clear the refusal on a delete nobody confirmed; `unknown` and a count, since
            # neither the outcome nor the names are this attempt's to describe.
            return DisposalFailure(
                "unknown", f"another disposal has not yet reported on {len(left)} container(s)"
            )
        return None

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        """Delete every container labelled ``(scope, thread_id)``: how many, and what stayed.

        The labels are the source of truth, because a conversation delete has to reach
        containers this process never created. The registry is the fallback for when the listing
        fails, and its entries are dropped either way.
        """
        mine = [k for k in list(self._registry) if k[0] == scope and k[1] == thread_id]
        remembered = [self._registry.pop(k) for k in mine]
        retained = {
            p: set(names)
            for p, names in self._undeleted.items()
            if p[0] == scope and p[1] == thread_id
        }
        swept = await self._purge(
            [(_LABEL_SCOPE, scope), (_LABEL_THREAD, thread_id)],
            fallback=list(
                dict.fromkeys(
                    [*remembered, *sorted(n for names in retained.values() for n in names)]
                )
            ),
            thread_id=thread_id,
        )
        # Nothing is subtracted here: the container this sweep removed and one recorded since
        # carry the same name, so taking one away drops the other.
        return ScopePurge(swept.count, swept.reason)

    async def _purge(
        self, label_filters: list[tuple[str, str]], fallback: list[str], thread_id: str
    ) -> _Sweep:
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
        queried = await self._list_names_by_labels(label_filters)
        listed = queried if queried is not None else []
        listed_set = set(listed)
        stranded = [n for n in fallback if n not in listed_set]
        names = [*listed, *stranded]

        count = 0
        undeleted: dict[str, DisposalFailure] = {}
        unlisted = None
        if queried is None:
            # The registry fallback still names what this process created, so those are swept.
            # A sweep that could not read the labels cannot claim to have reached a container
            # another replica created, which is the gap the labels exist to close.
            unlisted = DisposalFailure(
                "unlisted", "could not list containers, so the sweep may be partial"
            )
        for target in names:
            removal = await self._remove(target)
            if removal.removed and not target.endswith(_PROXY_SUFFIX):
                logger.info("sandbox released: container=%s thread=%s (purge)", target, thread_id)
                count += 1
            # Workload containers only. A proxy and a network carry no guest data, so one left
            # behind is an infrastructure leak to log rather than a reason to refuse the key.
            if removal.failure is not None and not target.endswith(_PROXY_SUFFIX):
                undeleted[target] = removal.failure

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
        return _Sweep(count, undeleted, unlisted)

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

    async def _remove(self, target: str) -> _Removal:
        """Force-remove ``target``. Never raises; reports what it did.

        A container docker says it does not have is a removal that has nothing to do, not a
        failure: the sweep tries names the registry remembers, and one already gone is the
        ordinary case.
        """
        # Dropped before the call, so a failed removal cannot leave stale facts behind.
        self._forget_facts(target)
        try:
            result = await self._docker(
                "rm", "-f", target, timeout=self._config.command_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.warning("docker backend: failed to remove container %s: %s", target, exc)
            # The invocation itself failed, so nothing was asked of the engine.
            return _Removal(
                removed=False, failure=DisposalFailure("unreachable", f"{target}: {exc}")
            )
        if result.returncode == 0:
            return _Removal(removed=True)
        if _NO_SUCH in result.stderr.lower():
            return _Removal(removed=False)
        logger.warning(
            "docker backend: failed to remove container %s: %s", target, result.stderr.strip()
        )
        # The engine answered and the container is still there.
        return _Removal(
            removed=False, failure=DisposalFailure("refused", f"{target}: {result.stderr.strip()}")
        )

    async def _list_names_by_labels(self, label_filters: list[tuple[str, str]]) -> list[str] | None:
        """Container names matching every filter, or ``None`` when the query failed.

        Read from docker; never raises.  Told apart because the paragraph below is otherwise
        the whole record: a failed listing and a conversation with nothing in it both come
        back empty, and only one of them means the sweep covered everything.

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
            return None
        if result.returncode != 0:
            logger.warning(
                "docker backend: could not list containers to purge: %s", result.stderr.strip()
            )
            return None
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
