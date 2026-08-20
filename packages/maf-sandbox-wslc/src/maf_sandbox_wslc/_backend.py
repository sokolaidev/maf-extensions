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
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxEntry,
    SandboxKey,
    SandboxSpec,
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

#: `wslc` exits non-zero for a container that is not there, so removal is judged by this.
_NOT_FOUND = "WSLC_E_CONTAINER_NOT_FOUND"

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


class _WslcRunner(Protocol):
    """The seam every ``wslc`` invocation goes through."""

    async def __call__(
        self, *args: str, stdin: bytes | None = None, timeout: float | None = None
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

    async def write_file(self, path: str, content: str | bytes) -> None:
        """Write ``content`` to ``path`` inside the container, parents included.

        Sent as a one-entry tar on stdin.  A ``cp`` destination must already exist and ``/``
        is the only path that always does, so the entry name carries the whole path and wslc
        creates the missing directories from it.

        ``str`` is encoded UTF-8 whatever the host's locale says; ``bytes`` is written as
        given, and is what an in-door carrying a PNG or a spreadsheet needs — the shape the
        :class:`~maf_sandbox.Sandbox` protocol promises and the docker backend already takes.
        """
        data = content.encode("utf-8") if isinstance(content, str) else content
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo(path.lstrip("/"))
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
            raise RuntimeError(f"wslc could not write {path}: {result.stderr_text.strip()}")

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        """Run ``command``, bounded by ``timeout``.

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
        try:
            result = await self._run(
                "container", "exec", "-w", working_directory, self._name, *argv, timeout=timeout
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

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Not supported: this backend declares no
        :data:`~maf_sandbox.Capability.FILES_DELETE`.

        Not for want of ``rm``: confining a removal means walking parents, and
        :meth:`stat_file` is the walk this backend has none of (#125).
        """
        raise NotImplementedError(
            "the wslc backend does not support FILES_DELETE: confining a removal needs the "
            "component walk that stat_file provides, and this backend has none. Remove through "
            "exec if the workload already requires it, or declare a backend with a pull surface."
        )


class WslcSandboxBackend:
    """Hands out container-isolated sandboxes from the WSL container CLI (``wslc``)."""

    def __init__(self, config: WslcSandboxConfig) -> None:
        self._config = config
        # (scope, thread_id, agent_dir, kind) -> name: a purge fallback for when the listing
        # fails, never the truth. Holds the last name acquired per key and kind, which is
        # enough to reclaim them.
        self._registry: dict[tuple[str, str, str, str], str] = {}
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
    def egress(self) -> Egress:
        # A capability, not a per-spec fact: the backend can enforce an allowlist iff it has a
        # proxy image to do it with. `acquire` still closes a spec that allows nothing outright.
        if self._config.egress_proxy_image:
            return Egress.ALLOWLIST
        return Egress.CLOSED

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.EXEC, Capability.FILES_IN})

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
        fails, and its entries are dropped either way: an entry pointing at a container that may
        already be gone is worse than no entry.
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

        A proxy carries its sandbox's labels, so it is listed and removed alongside it, but it is
        not a sandbox and is not counted. Its network is removed after it, when it is free to go.
        The ``fallback`` names cover the case the listing failed.

        When this backend enforces allowlists, every workload's proxy and network are swept —
        not only those whose proxy the listing returned — so a proxy a reuse failed to rebuild,
        or one deleted by hand, does not strand its network. A closed backend does no such sweep:
        its sandboxes never had a network, and a listed proxy from an earlier allowlisted run
        still takes its own network with it below.
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
        if self._config.egress_proxy_image is not None:
            for workload in (n for n in names if not n.endswith(_PROXY_SUFFIX)):
                if _proxy_name(workload) not in listed_set:
                    await self._remove(_proxy_name(workload))
                networks.add(_network_name(workload))
        for net in networks:
            await self._remove_network(net)
        return count

    # -- internals ----------------------------------------------------------------

    async def _wslc(
        self, *args: str, stdin: bytes | None = None, timeout: float | None = None
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
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
        except BaseException:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        return _WslcResult(process.returncode or 0, stdout, stderr)

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

    async def _remove(self, target: str) -> bool:
        """Force-remove ``target``. Returns whether it removed one; never raises."""
        try:
            result = await self._wslc(
                "container", "remove", "-f", target, timeout=self._config.command_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.warning("wslc backend: failed to remove container %s: %s", target, exc)
            return False
        if result.returncode == 0:
            return True
        if _NOT_FOUND not in result.stderr_text:
            logger.warning(
                "wslc backend: failed to remove container %s: %s",
                target,
                result.stderr_text.strip(),
            )
        return False

    async def _list_names_by_labels(self, label_filters: list[tuple[str, str]]) -> list[str]:
        """Container names matching every ``(label, value)`` filter, read from wslc. Never raises.

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
            return []
        if result.returncode != 0:
            logger.warning(
                "wslc backend: could not list containers to purge: %s", result.stderr_text.strip()
            )
            return []
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
