"""The wslc backend: :class:`~maf_sandbox.SandboxBackend` on WSL containers.

Everything provider-specific lives here — the command line, the naming and labelling scheme,
the egress policy and the label-based purge.  A workload above the router sees only
``write_file`` and ``exec``.

Isolation is :data:`~maf_sandbox.Isolation.CONTAINER`: a container shares the host kernel and
sits on the developer's own machine, below the router's default
:data:`~maf_sandbox.Isolation.MICROVM` floor.  A host that wants this backend opts the floor
down explicitly with ``min_isolation=Isolation.CONTAINER``; with nothing passed, construction
raises :class:`~maf_sandbox.SandboxBackendNotPermitted`.  That refusal is the point of the
declaration, not a limitation to work around — there is no flag left to forget.

Egress is :data:`~maf_sandbox.Egress.CLOSED` — every container is created ``--network none``.
The CLI cannot allow one host and deny the rest, and confining *more* than a spec asks only
makes a workload fail loudly at whatever it could not fetch, which is why the router permits
it with a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import posixpath
import re
import tarfile
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol, cast

from maf_sandbox import (
    BackendDeclarations,
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
    SandboxSpec,
    ScopePurge,
    fold_disposal_failures,
)
from maf_sandbox.paths import (
    confine_guest_write_path,
    sandbox_entry_from_tar_header,
    tar_header_from_block,
)

from ._config import WslcSandboxConfig
from ._proxy import build_context

logger = logging.getLogger(__name__)

__all__ = ["BACKEND_NAME", "WslcSandboxBackend"]

#: The name :attr:`WslcSandboxBackend.name` answers to, and the value
#: :class:`~maf_sandbox.SandboxRouter`'s ``selected=`` matches on.
#:
#: Public because a host choosing a backend from its own configuration needs the value before
#: it has a backend to read it off, and building one to learn a constant is a lot of machinery
#: for a fixed string (#411). The property below returns this, so the two cannot disagree.
#:
#: Worth having even though this backend runs on one platform: a host that registers it on
#: Windows and something else elsewhere still selects by name, and that selection is written
#: where the platform check is, not where the backend is built.
#:
#: Import it qualified or aliased when more than one backend package is in play. Every backend
#: exports this same symbol, so two `from … import BACKEND_NAME` lines shadow each other and
#: the second wins silently. Either `import maf_sandbox_wslc` and reach it as
#: `maf_sandbox_wslc.BACKEND_NAME`, or alias at the import:
#: `from maf_sandbox_wslc import BACKEND_NAME as WSLC_BACKEND`.
BACKEND_NAME = "wslc"

# Written at create and read back on purge, so wslc is the durable record, not this process.
_LABEL_SCOPE = "maf-sandbox.scope"
_LABEL_THREAD = "maf-sandbox.thread"
_LABEL_AGENT = "maf-sandbox.agent"
_LABEL_KIND = "maf-sandbox.kind"
_LABEL_PREFIX = "maf-sandbox.label."

_LABEL_VALUE_MAX = 63
_LABEL_VALUE_SAFE = re.compile(r"[A-Za-z0-9._-]+")
_LABEL_VALUE_DIGEST = re.compile(r"sha256-[0-9a-f]{48}")

# The narrowest set any shipped backend declares: no pull surface, no removal, no run_code.
_CAPABILITIES = frozenset({Capability.EXEC, Capability.FILES_IN})

#: `wslc` exits non-zero for a container that is not there, so removal is judged by this.
_NOT_FOUND = "WSLC_E_CONTAINER_NOT_FOUND"
# `container cp` reports a missing guest path with this code on the live CLI.
_PATH_NOT_FOUND = "ERROR_PATH_NOT_FOUND"
# A directory cannot be streamed to stdout, but this diagnostic proves it exists and is a directory.
_DIRECTORY_COPY_ERROR = "cannot copy a directory to a file path"
# `container cp` uses the docker engine's wording; confirm this branch on a live WSL host.
_NO_SUCH = "no such"

#: What `run --name` reports when the name is taken — the one create failure that is recoverable.
#: `network create` reports the same code for a taken network name.
_ALREADY_EXISTS = "ERROR_ALREADY_EXISTS"

#: `network remove` says this when the network is already gone — a no-op, not a failure.
_NETWORK_NOT_FOUND = "not found"

#: Marks the egress proxy so a purge can tell it from the sandboxes it counts.
_LABEL_ROLE = "maf-sandbox.role"

_PROXY_PORT = 3128
_ALLOW_ENV = "MAF_SANDBOX_ALLOW"
_PROXY_READY_MARKER = "listening"
_PROXY_READY_ATTEMPTS = 20
_PROXY_READY_DELAY_S = 0.25


def _label_value(raw: str) -> str:
    """A label value that survives ``-l key=value``, and means the same on create and purge.

    Short, plain values pass through so a listing stays readable; anything longer, empty, or
    carrying a character that would split the argument becomes a digest.  Truncation is
    deliberately **not** used: two scopes sharing a prefix would land on the same label, and
    these labels are what :meth:`WslcSandboxBackend.dispose_scope` selects on, so a collision
    would let one conversation's purge delete another's containers.

    A value already shaped like a digest is digested too, rather than passed through: it is a
    legal short plain value, so passing it through would let a caller name a scope that lands
    on the label some other scope's digest produced — the same collision, hand-made.

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
    """The labels a container is created with — the same ones `dispose_scope` selects on."""
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
    first one's network policy — the collision the sandbox-identity invariant exists to
    prevent.  ``egress_id`` folds the egress configuration in for the same reason: a sandbox
    is reused only by an acquire that wants the *same* egress, so a change of mode or of
    allowed hosts gets its own container rather than silently keeping one whose network no
    longer matches what the backend now declares.  It is empty for closed egress.
    """
    parts = [key.scope, key.thread_id, key.agent_dir, kind]
    if egress_id:
        parts.append(egress_id)
    digest = sha256("|".join(parts).encode("utf-8"))
    return f"maf-sandbox-wslc-{digest.hexdigest()[:12]}"


_NET_SUFFIX = "-net"
_PROXY_SUFFIX = "-proxy"


def _network_name(container: str) -> str:
    """The internal network paired with a sandbox container, derived from its name."""
    return f"{container}{_NET_SUFFIX}"


def _proxy_name(container: str) -> str:
    """The egress proxy paired with a sandbox container, derived from its name."""
    return f"{container}{_PROXY_SUFFIX}"


_TAR_BLOCK = 512


def _listed_names(payload: str) -> list[str]:
    """The container names in a ``container list --format json`` payload."""
    try:
        parsed: object = json.loads(payload or "[]")
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    names: list[str] = []
    for row in cast("list[object]", parsed):
        if not isinstance(row, dict):
            continue
        value = cast("dict[str, object]", row).get("Name")
        if isinstance(value, str):
            names.append(value)
    return names


@dataclass(frozen=True)
class _WslcResult:
    """What one ``wslc`` invocation returned.

    ``stdout``/``stderr`` are the raw bytes the process wrote.  Decoding used to happen here,
    unconditionally and with ``errors="replace"``, which silently substituted characters in
    any output that was not text — a command whose stdout is binary came back corrupted with
    no exception raised.  Callers that want text ask for it, via :attr:`stdout_text`.
    """

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        """``stdout`` decoded leniently — a diagnostic should never raise on a stray byte."""
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        """``stderr`` decoded leniently — a diagnostic should never raise on a stray byte."""
        return self.stderr.decode("utf-8", errors="replace")


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


class _WslcRunner(Protocol):
    """The seam every ``wslc`` invocation goes through."""

    async def __call__(
        self,
        *args: str,
        stdin: bytes | None = None,
        timeout: float | None = None,
        read_limit: int | None = None,
    ) -> _WslcResult: ...


class _WslcSandbox:
    """A running container, narrowed to what a workload is allowed to do with it."""

    def __init__(self, run: _WslcRunner, name: str, command_timeout: float) -> None:
        self._run = run
        self._name = name
        self._command_timeout = command_timeout

    @property
    def container_name(self) -> str:
        return self._name

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        """Write ``content`` to ``path`` inside the container, parents included.

        Sent as a one-entry tar on stdin.  A ``cp`` destination must already exist and ``/``
        is the only path that always does, so the entry name carries the whole path and wslc
        creates the missing directories from it.

        ``str`` is encoded UTF-8 whatever the host's locale says; ``bytes`` is written as
        given, and is what an in-door carrying a PNG or a spreadsheet needs — the shape the
        :class:`~maf_sandbox.Sandbox` protocol promises and the docker backend already takes.
        """
        guest = await confine_guest_write_path(
            lambda p: self._stat_guest(p, p), path, working_directory
        )
        data = content.encode("utf-8") if isinstance(content, str) else content
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo(guest.lstrip("/"))
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))

        result = await self._run(
            "container",
            "cp",
            "-",
            f"{self._name}:/",
            stdin=buffer.getvalue(),
            timeout=self._command_timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"wslc could not write {guest}: {result.stderr_text.strip()}")

    async def _stat_guest(self, guest: str, rel: str) -> SandboxEntry | None:
        """Stat an absolute guest path from the first container-cp tar header."""
        result = await self._run(
            "container",
            "cp",
            f"{self._name}:{guest}",
            "-",
            timeout=self._command_timeout,
            read_limit=_TAR_BLOCK,
        )
        if result.returncode != 0 and not result.stdout:
            error_text = result.stderr_text.lower()
            if _NO_SUCH in error_text or _PATH_NOT_FOUND.lower() in error_text:
                return None
            if _DIRECTORY_COPY_ERROR in error_text:
                return SandboxEntry(path=rel, kind=EntryKind.DIRECTORY, size_bytes=None)
            raise RuntimeError(f"wslc could not stat {rel}: {result.stderr_text.strip()}")
        if result.returncode != 0:
            # A failed copy that streamed bytes is not a stat: the header would be a body's
            # first 512 bytes read as tar, so refuse rather than classify them.
            raise RuntimeError(f"wslc could not stat {rel}: {result.stderr_text.strip()}")
        if len(result.stdout) < _TAR_BLOCK:
            # WSLC streams an empty response for regular files and links. Probe the entry type
            # without following it; the tar header remains the fast path where the CLI provides one.
            for flag, kind in (
                ("-L", EntryKind.SYMLINK),
                ("-d", EntryKind.DIRECTORY),
                ("-f", EntryKind.FILE),
            ):
                probe = await self._run(
                    "container",
                    "exec",
                    self._name,
                    "test",
                    flag,
                    guest,
                    timeout=self._command_timeout,
                )
                if probe.returncode == 0:
                    return SandboxEntry(path=rel, kind=kind, size_bytes=None)
            raise RuntimeError(f"wslc returned no tar header for {rel}")
        return sandbox_entry_from_tar_header(tar_header_from_block(result.stdout[:_TAR_BLOCK]), rel)

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        """Run ``command`` as the image's user, bounded by ``timeout``.

        ``wslc exec`` takes argv natively, so a sequence goes through element for element with
        no shell and nothing to quote; a string is a shell command line and runs as ``sh -c``.

        ``timeout`` bounds the host-side call, not the command.  Killing the ``wslc exec``
        process does not reach the process it started *inside* the container, and there is no
        per-command handle to kill, so a timed-out call discards the whole sandbox before
        ``TimeoutError`` propagates — a workload reports the hang as a diagnostic, and the next
        acquire pays a fresh create.  A **cancelled** call still reaps the host-side process
        but keeps the sandbox: the in-container command runs on until the sandbox is disposed.
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
        """One ``wslc container exec``, as the image's user or as ``--user 0``.

        :meth:`exec` is the guest program's own and names no user; :meth:`reclaim` asks for root,
        because the file plane (:meth:`write_file`) writes as the host authority and the image's
        user cannot remove what a call left behind on a non-root image.  See
        ``docs/sandbox/backends/wslc.md``.
        """
        privilege = ("--user", "0") if as_root else ()
        try:
            result = await self._run(
                "container",
                "exec",
                *privilege,
                "-w",
                working_directory,
                self._name,
                *argv,
                timeout=timeout,
            )
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self._run(
                    "container", "remove", "-f", self._name, timeout=self._command_timeout
                )
            raise
        return ExecResult(
            stdout=result.stdout_text, stderr=result.stderr_text, exit_code=result.returncode
        )

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        """Not supported: this backend declares neither :data:`~maf_sandbox.Capability.FILES_OUT`
        nor :data:`~maf_sandbox.Capability.FILES_LIST`.

        ``wslc`` shares the docker engine's ``container cp`` tar stream, so a stat-from-header
        path is buildable here — but it was never wired, and the router refuses a spec requiring
        either capability before a workload runs, so a well-formed caller never reaches here.
        The raise is the honest floor under one that skipped the check: an :class:`AttributeError`
        from a missing method names neither the backend nor the file, and reads as unrelated to
        a ``write_file`` that had just succeeded.  See :mod:`maf_sandbox.conformance` for the
        duty a backend that *does* declare ``FILES_OUT`` is held to.
        """
        raise NotImplementedError(
            "the wslc backend does not support FILES_OUT or FILES_LIST: declare a backend that "
            "does, or require only exec and FILES_IN."
        )

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        """Not supported: this backend declares neither :data:`~maf_sandbox.Capability.FILES_OUT`
        nor :data:`~maf_sandbox.Capability.FILES_LIST`.  See :meth:`stat_file`.
        """
        raise NotImplementedError(
            "the wslc backend does not support FILES_OUT or FILES_LIST: declare a backend that "
            "does, or require only exec and FILES_IN."
        )

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        """Not supported: this backend declares neither :data:`~maf_sandbox.Capability.FILES_OUT`
        nor :data:`~maf_sandbox.Capability.FILES_LIST`.  See :meth:`stat_file`.
        """
        raise NotImplementedError(
            "the wslc backend does not support FILES_OUT or FILES_LIST: declare a backend that "
            "does, or require only exec and FILES_IN."
        )

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        """Not supported: this backend declares no :data:`~maf_sandbox.Capability.RUN_CODE`.

        Not for want of an interpreter — the image may well carry one — but because *which*
        runtime an image carries is a property of the image, and this backend is handed image
        references it does not parse. Declaring the capability would be a claim about someone
        else's artefact. A workload that wants a runtime by name invokes it through
        :meth:`exec` and owns that assumption itself.
        """
        raise NotImplementedError(
            "the wslc backend does not support RUN_CODE: evaluating code without a shell "
            "means knowing which runtime the guest carries, and this backend resolves an "
            "image reference without looking inside it. Run the interpreter through exec, or "
            "register a backend that declares RUN_CODE."
        )

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Not supported: this backend declares no
        :data:`~maf_sandbox.Capability.FILES_DELETE`.

        Not for want of ``rm``: confining a removal means checking every ancestor, and this
        backend does run that check for :meth:`write_file`, over its private ``_stat_guest``.
        What that stat cannot do is classify a link without asking the container (#495), which
        is a different thing to rest a recursive delete on than a write. Which of that and the
        absent pull surface (#125) is the blocker is #743.
        """
        raise NotImplementedError(
            "the wslc backend does not support FILES_DELETE: confining a removal needs the "
            "component walk that stat_file provides, and this backend has none. Remove through "
            "exec if the workload already requires it, or declare a backend with a pull surface."
        )

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Remove ``directory`` with ``rm -rf`` over :meth:`_exec`, as ``--user 0``.

        The file plane (:meth:`write_file`) writes as the host authority, so on a non-root image
        the image's user cannot remove what a call left behind.  Root is always correct here
        because the caller made ``directory``: no filesystem path check is owed at all here,
        which is why this member is served where :meth:`remove` is not. Runs from ``/`` because
        ``working_directory`` may not exist.

        Raises:
            ValueError: A path that is not absolute, or fewer than two components from the
                root.
        """
        del working_directory
        if not directory.startswith("/"):
            raise ValueError(f"refusing to reclaim a path that is not absolute: {directory}")
        target = posixpath.normpath(directory)
        if len([part for part in target.split("/") if part]) < 2:
            raise ValueError(f"refusing to reclaim recursively that close to the root: {target}")
        removed = await self._exec(
            ["rm", "-rf", "--", target],
            working_directory="/",
            timeout=timeout,
            as_root=True,
        )
        if removed.exit_code != 0:
            raise OSError(
                f"could not reclaim {directory}: rm exited {removed.exit_code}"
                f"{f' — {removed.stderr.strip()}' if removed.stderr else ''}"
            )


class WslcSandboxBackend:
    """Hands out container-isolated sandboxes from the WSL container CLI (``wslc``)."""

    def __init__(self, config: WslcSandboxConfig) -> None:
        self._config = config
        # Built once: every input is fixed here, and the router reads the object on each
        # `ensure_can_serve` and each `acquire`. Only `egress_modes` reads the config at all —
        # with a proxy image this backend can allowlist named hosts or deny all, and without
        # one it can only close. Never UNRESTRICTED: a container backend always cuts or
        # proxies. `limits` is left at its default, which is the ceiling this backend accepts.
        self._declarations = BackendDeclarations(
            capabilities=_CAPABILITIES,
            egress_modes=frozenset({Egress.ALLOWLIST, Egress.CLOSED})
            if config.egress_proxy_image
            else frozenset({Egress.CLOSED}),
        )
        # (scope, thread_id, agent_dir, kind) -> name: a purge fallback for when the listing
        # fails, never the truth. Holds the last name acquired per key and kind, which is
        # enough to reclaim them.
        self._registry: dict[tuple[str, str, str, str], str] = {}
        #: Workload containers a removal could not take away, by key prefix. Retry
        #: bookkeeping only: it does **not** keep one from being served, since the name comes
        #: from the key and `acquire` asks the engine. Refusing to serve is the router's
        #: ledger. A `dispose` clears an entry once the removal lands; a scope purge never
        #: does, because a name is not a generation and it cannot tell the two apart.
        self._undeleted: dict[tuple[str, str, str], set[str]] = {}
        # Get-or-create serialised per (running loop, key): a create names no container until it
        # returns, so two acquires racing one key would each build a network, a proxy and a
        # sandbox. Per loop because an asyncio.Lock binds to the loop that first waits on it, and
        # weak-keyed on the loop so a process that runs a loop per call (asyncio.run) does not
        # accumulate a lock table for loops long dead.
        self._acquire_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[tuple[str, str, str, str], asyncio.Lock]
        ] = weakref.WeakKeyDictionary()

    @property
    def name(self) -> str:
        return BACKEND_NAME

    @property
    def isolation(self) -> Isolation:
        return Isolation.CONTAINER

    @property
    def declarations(self) -> BackendDeclarations:
        return self._declarations

    # -- SandboxBackend -----------------------------------------------------------

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> _WslcSandbox:
        """Return a running container for ``key``, reusing a warm one when there is one.

        The egress scaffolding is (re-)ensured on every acquire, not only on create: a proxy a
        host reboot stopped, or one a crashed setup left half-connected, is rebuilt here rather
        than leaving a sandbox that declares an allowlist and enforces nothing. Reused, restarted
        and created are logged at INFO — the difference is a warm exec versus a fresh image start.
        """
        egress_id = self._egress_id(spec)
        name = _container_name(key, spec.kind, egress_id)
        async with self._acquire_lock(key, spec.kind):
            running = await self._is_listed(name, all_states=False)
            stopped = not running and await self._is_listed(name, all_states=True)
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
            return _WslcSandbox(self._wslc, name, self._config.command_timeout_seconds)

    async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
        """Delete every container for ``key`` — every kind, closed or allowlisted — with
        proxies and networks.

        By label, so it reaches a sandbox created under an egress configuration this backend no
        longer runs; the registry name is the fallback for when the listing itself fails. Never
        raises: a removal that failed comes back as the reason, so the router can refuse a key
        whose data is still sitting in a container.
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
        fails, and its entries are dropped either way: an entry pointing at a container that may
        already be gone is worse than no entry.
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

        A proxy carries its sandbox's labels, so it is listed and removed alongside it, but it is
        not a sandbox and is not counted. Its network is removed after it, when it is free to go.
        The ``fallback`` names cover the case the listing failed.

        When this backend enforces allowlists, every workload's proxy and network are swept —
        not only those whose proxy the listing returned — so a proxy a reuse failed to rebuild,
        or one deleted by hand, does not strand its network. A closed backend does no such sweep:
        its sandboxes never had a network, and a listed proxy from an earlier allowlisted run
        still takes its own network with it below.
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
        if self._config.egress_proxy_image is not None:
            for workload in (n for n in names if not n.endswith(_PROXY_SUFFIX)):
                if _proxy_name(workload) not in listed_set:
                    await self._remove(_proxy_name(workload))
                networks.add(_network_name(workload))
        for net in networks:
            await self._remove_network(net)
        return _Sweep(count, undeleted, unlisted)

    # -- internals ----------------------------------------------------------------

    async def _wslc(
        self,
        *args: str,
        stdin: bytes | None = None,
        timeout: float | None = None,
        read_limit: int | None = None,
    ) -> _WslcResult:
        """Run one ``wslc`` command — the single seam every invocation goes through.

        Any abnormal end to the wait — a timeout, a cancelled caller — kills the subprocess and
        reaps it before the exception propagates, so a command that stopped answering, or one
        whose caller went away, cannot outlive the call that made it.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                self._config.wslc_path,
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except NotImplementedError as exc:
            # A selector loop on Windows raises this with no message at all.
            raise ValueError(
                "the wslc backend needs an event loop that can spawn subprocesses — on Windows "
                "that is asyncio's default Proactor loop"
            ) from exc
        try:
            if read_limit is None:
                stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
            else:
                if stdin is not None and process.stdin is not None:
                    process.stdin.write(stdin)
                    await process.stdin.drain()
                    process.stdin.close()
                stdout, stderr = await self._read_bounded(process, read_limit, timeout)
        except BaseException:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        return _WslcResult(process.returncode or 0, stdout, stderr)

    @staticmethod
    async def _read_bounded(
        process: asyncio.subprocess.Process, read_limit: int, timeout: float | None
    ) -> tuple[bytes, bytes]:
        """Read a bounded stdout head, then kill and reap before collecting stderr."""
        assert process.stdout is not None and process.stderr is not None
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
            stderr = b""
        with contextlib.suppress(Exception):
            await process.wait()
        return stdout, stderr

    async def _is_listed(self, name: str, *, all_states: bool) -> bool:
        """Whether ``name`` is listed — running only, or in any state.

        Two narrow queries rather than one that reads a state field: ``--filter name=`` is a
        substring match and the JSON state is an undocumented integer, so the only claim worth
        making is that an exact name appears in the running listing.
        """
        args = ["container", "list"]
        if all_states:
            args.append("-a")
        args += ["--format", "json", "--filter", f"name={name}"]

        result = await self._wslc(*args, timeout=self._config.command_timeout_seconds)
        return result.returncode == 0 and name in _listed_names(result.stdout_text)

    async def _restart(self, name: str) -> bool:
        """Start an existing container, removing it if it will not start.

        The name is what a replacement ``run`` needs back; leaving a broken container under it
        would fail every acquire from here on.
        """
        result = await self._wslc(
            "container", "start", name, timeout=self._config.command_timeout_seconds
        )
        if result.returncode == 0:
            return True
        logger.info(
            "container %s did not start (%s); creating a replacement",
            name,
            result.stderr_text.strip(),
        )
        await self._remove(name)
        return False

    def _egress_id(self, spec: SandboxSpec) -> str:
        """The egress folded into a sandbox's identity — empty when it has no allowlist to keep.

        A proxy image plus a non-empty allowlist means allowlisted egress; either missing means
        closed, and closed is the empty string so a closed sandbox keeps its historical name. An
        allowlist of nothing is closed too — ``--network none`` denies everything for free,
        without burning a network slot on a proxy that would allow the same nothing.
        """
        if self._config.egress_proxy_image is None or not spec.egress_allow:
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

    async def _create_workload(
        self, name: str, key: SandboxKey, spec: SandboxSpec, *, allowlisting: bool
    ) -> str:
        """Create and start the workload container; returns the image it ran.

        The network and proxy already exist by now (``_ensure_egress`` ran first), so this only
        places the workload: on ``--network none`` when closed, or on the internal network with
        the proxy in its environment when allowlisting.
        """
        image = spec.image_id or spec.image
        if not image:
            raise ValueError(
                "No sandbox image is configured: the spec names neither image nor image_id."
            )

        args = ["container", "run", "-d", "--name", name]
        if allowlisting:
            proxy_url = f"http://{_proxy_name(name)}:{_PROXY_PORT}"
            args += ["--network", _network_name(name)]
            args += ["-e", f"HTTPS_PROXY={proxy_url}", "-e", f"HTTP_PROXY={proxy_url}"]
        else:
            args += ["--network", "none"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["-l", f"{label}={value}"]
        args += [image, "sleep", "infinity"]

        result = await self._wslc(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0:
            if _ALREADY_EXISTS in result.stderr_text and await self._adopt(name):
                logger.info("container %s already existed; adopted it instead of creating", name)
                return image
            raise RuntimeError(
                f"wslc could not create container {name}: {result.stderr_text.strip()}"
            )
        return image

    async def _ensure_egress(
        self, name: str, key: SandboxKey, spec: SandboxSpec, *, fresh: bool
    ) -> None:
        """Build (or repair) the internal network and filtering proxy for an allowlisted sandbox.

        ``fresh`` says no workload is attached yet, so if the proxy cannot be brought up the
        network this just created is ours to reclaim rather than leak; once a warm workload is on
        it, the network stays and the proxy failure surfaces to the caller instead.
        """
        net = _network_name(name)
        await self._ensure_network(net, key, spec)
        try:
            await self._ensure_proxy(name, key, spec)
        except BaseException:
            if fresh:
                await self._remove_network(net)
            raise

    async def _ensure_network(self, net: str, key: SandboxKey, spec: SandboxSpec) -> None:
        """Create the sandbox's internal network, adopting one already there."""
        args = ["network", "create", "--internal"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["-l", f"{label}={value}"]
        args.append(net)
        result = await self._wslc(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0 and _ALREADY_EXISTS not in result.stderr_text:
            raise RuntimeError(f"wslc could not create network {net}: {result.stderr_text.strip()}")

    async def _ensure_proxy(self, name: str, key: SandboxKey, spec: SandboxSpec) -> None:
        """Put a fresh filtering proxy on the sandbox's network, dual-homed and confirmed listening.

        Recreated every acquire, not adopted: a fresh proxy always carries the current spec's
        allowlist, always gets its outbound leg connected, and its log holds only this run's
        readiness line — so a stale, half-connected or wrong-allowlist proxy is never mistaken
        for a working one.
        """
        proxy_image = cast("str", self._config.egress_proxy_image)
        proxy = _proxy_name(name)
        await self._remove(proxy)

        args = ["container", "run", "-d", "--name", proxy, "--network", _network_name(name)]
        args += ["-e", f"{_ALLOW_ENV}={','.join(spec.egress_allow)}"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["-l", f"{label}={value}"]
        args += ["-l", f"{_LABEL_ROLE}=proxy", proxy_image]

        result = await self._wslc(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0:
            raise RuntimeError(
                f"wslc could not start the egress proxy {proxy}: {result.stderr_text.strip()} — "
                f"if the image is missing, build it: wslc build -t {proxy_image} {build_context()}"
            )

        connect = await self._wslc(
            "network", "connect", "bridge", proxy, timeout=self._config.command_timeout_seconds
        )
        if connect.returncode != 0:
            # A proxy without its egress leg would turn ALLOWLIST into CLOSED silently.
            raise RuntimeError(
                f"wslc could not give the egress proxy {proxy} its outbound leg: "
                f"{connect.stderr_text.strip()}"
            )
        await self._await_listening(proxy)

    async def _await_listening(self, proxy: str) -> None:
        """Wait for the proxy's listening line; fail the acquire if it never comes.

        A proxy that has not bound its port yet would let the workload's first request through to
        nothing and read as a network error. Rather than hand back a sandbox whose egress is not
        actually up, the acquire fails here and the caller can retry — the network is reclaimed on
        the way out when this was a fresh create.
        """
        for _ in range(_PROXY_READY_ATTEMPTS):
            result = await self._wslc(
                "container", "logs", proxy, timeout=self._config.command_timeout_seconds
            )
            if result.returncode == 0 and _PROXY_READY_MARKER in result.stdout_text:
                return
            await asyncio.sleep(_PROXY_READY_DELAY_S)
        raise RuntimeError(f"egress proxy {proxy} never reported listening")

    async def _adopt(self, name: str) -> bool:
        """Whether an existing ``name`` is running, or could be started — the reuse path again.

        The listing that sent ``acquire`` down the create branch can be out of date by the time
        ``run`` executes: two acquires for one key race, or a transient listing failure hides a
        container that is right there.  Without this the name stays taken and every acquire for
        that key fails from then on.
        """
        if await self._is_listed(name, all_states=False):
            return True
        return await self._is_listed(name, all_states=True) and await self._restart(name)

    async def _remove(self, target: str) -> _Removal:
        """Force-remove ``target``. Never raises; reports what it did.

        A container wslc says it does not have is a removal that has nothing to do, not a
        failure: the sweep tries names the registry remembers, and one already gone is the
        ordinary case.
        """
        try:
            result = await self._wslc(
                "container", "remove", "-f", target, timeout=self._config.command_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.warning("wslc backend: failed to remove container %s: %s", target, exc)
            # The invocation itself failed, so nothing was asked of the engine.
            return _Removal(
                removed=False, failure=DisposalFailure("unreachable", f"{target}: {exc}")
            )
        if result.returncode == 0:
            return _Removal(removed=True)
        if _NOT_FOUND in result.stderr_text:
            return _Removal(removed=False)
        logger.warning(
            "wslc backend: failed to remove container %s: %s",
            target,
            result.stderr_text.strip(),
        )
        # The engine answered and the container is still there.
        return _Removal(
            removed=False,
            failure=DisposalFailure("refused", f"{target}: {result.stderr_text.strip()}"),
        )

    async def _list_names_by_labels(self, label_filters: list[tuple[str, str]]) -> list[str] | None:
        """Container names matching every filter, or ``None`` when the query failed.

        Read from wslc; never raises.  Told apart because the paragraph below is otherwise
        the whole record: a failed listing and a conversation with nothing in it both come
        back empty, and only one of them means the sweep covered everything.

        By name rather than id, because the proxy/network pairing is expressed in the names and
        ``container list`` does not report labels back.  ``_label_value`` on both sides, always:
        these have to be the same strings the create wrote, or the query matches nothing and
        every container for the deleted conversation keeps running — silently, since "found
        none" and "there were none" are one result.
        """
        args = ["container", "list", "-a", "--format", "json"]
        for label, value in label_filters:
            args += ["--filter", f"label={label}={_label_value(value)}"]
        try:
            result = await self._wslc(*args, timeout=self._config.command_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - purge must never fail
            logger.warning("wslc backend: could not list containers to purge: %s", exc)
            return None
        if result.returncode != 0:
            logger.warning(
                "wslc backend: could not list containers to purge: %s", result.stderr_text.strip()
            )
            return None
        return _listed_names(result.stdout_text)

    async def _remove_network(self, net: str) -> bool:
        """Force-remove a network. Returns whether it removed one; never raises.

        A network that was never there is a no-op, not a failure — an allowlisting backend's
        purge tries a workload's network whether or not that workload turns out to have had one.
        """
        try:
            result = await self._wslc(
                "network", "remove", net, timeout=self._config.command_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.warning("wslc backend: failed to remove network %s: %s", net, exc)
            return False
        if result.returncode == 0:
            return True
        if _NETWORK_NOT_FOUND not in result.stderr_text.lower():
            logger.warning(
                "wslc backend: failed to remove network %s: %s", net, result.stderr_text.strip()
            )
        return False


# The package's strict pyright pass type-checks this assignment. ``runtime_checkable`` only
# tests member *presence*, so ``isinstance(..., SandboxBackend)`` passes while a signature
# narrows (``write_file`` refusing ``bytes``) or a method goes missing (the pull surface) — the
# annotation is what fails the build instead, which is how #370 was caught at a sample call
# site, and this line holds it inside this package, where the gap was. A ``cast(...)`` would
# not: it is an unchecked escape hatch, accepted silently even when the types do not match.
# ``_`` is the conventional throwaway — the annotation is the load-bearing part, not the name —
# and the tuple poses both checks in one binding rather than two unused globals. Guarded by
# ``TYPE_CHECKING`` so the construction never runs.
if TYPE_CHECKING:
    _: tuple[SandboxBackend, type[Sandbox]] = (
        WslcSandboxBackend(WslcSandboxConfig()),
        _WslcSandbox,
    )
