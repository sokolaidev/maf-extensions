"""The wslc backend: :class:`~maf_sandbox.SandboxBackend` on WSL containers.

Everything provider-specific lives here — the command line, the naming and labelling scheme,
the egress policy and the label-based purge.  A workload above the router sees only
``write_file`` and ``exec``.

Isolation is :data:`~maf_sandbox.Isolation.CONTAINER`: a container shares the host kernel and
sits on the developer's own machine, so the router refuses this backend outright when the host
reports it is running deployed.  That refusal is the point of the declaration, not a
limitation to work around.

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
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, cast

from maf_sandbox import Egress, ExecResult, Isolation, SandboxKey, SandboxSpec

from ._config import WslcSandboxConfig
from ._proxy import build_context

logger = logging.getLogger(__name__)

__all__ = ["WslcSandboxBackend"]

# Written at create and read back on purge, so wslc is the durable record, not this process.
_LABEL_SCOPE = "maf-sandbox.scope"
_LABEL_THREAD = "maf-sandbox.thread"
_LABEL_AGENT = "maf-sandbox.agent"
_LABEL_PREFIX = "maf-sandbox.label."

_LABEL_VALUE_MAX = 63
_LABEL_VALUE_SAFE = re.compile(r"[A-Za-z0-9._-]+")
_LABEL_VALUE_DIGEST = re.compile(r"sha256-[0-9a-f]{48}")

#: `wslc` exits non-zero for a container that is not there, so removal is judged by this.
_NOT_FOUND = "WSLC_E_CONTAINER_NOT_FOUND"

#: What `run --name` reports when the name is taken — the one create failure that is recoverable.
#: `network create` reports the same code for a taken network name.
_ALREADY_EXISTS = "ERROR_ALREADY_EXISTS"

#: Marks the egress proxy so `dispose_scope` can tell it from the sandboxes it counts.
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
        **{f"{_LABEL_PREFIX}{k}": _label_value(v) for k, v in spec.labels.items()},
    }


def _container_name(key: SandboxKey) -> str:
    """The one container name a key maps to — derived, so acquire and dispose agree on it."""
    digest = sha256("|".join((key.scope, key.thread_id, key.agent_dir)).encode("utf-8"))
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
    """What one ``wslc`` invocation returned."""

    returncode: int
    stdout: str
    stderr: str


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

    async def write_file(self, path: str, content: str) -> None:
        """Write ``content`` to ``path`` inside the container, parents included.

        Sent as a one-entry tar on stdin.  A ``cp`` destination must already exist and ``/``
        is the only path that always does, so the entry name carries the whole path and wslc
        creates the missing directories from it.
        """
        data = content.encode("utf-8")
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
            raise RuntimeError(f"wslc could not write {path}: {result.stderr.strip()}")

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
        return ExecResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)


class WslcSandboxBackend:
    """Hands out container-isolated sandboxes from the WSL container CLI (``wslc``)."""

    def __init__(self, config: WslcSandboxConfig) -> None:
        self._config = config
        # (scope, thread_id, agent_dir) -> name: a `dispose_scope` fallback, never the truth.
        self._registry: dict[tuple[str, str, str], str] = {}

    @property
    def name(self) -> str:
        return "wslc"

    @property
    def isolation(self) -> str:
        return Isolation.CONTAINER

    @property
    def egress(self) -> str:
        # CLOSED is true because `_create` passes `--network none`; ALLOWLIST is true because
        # the same setting that flips this reply also builds the topology that enforces it.
        if self._config.egress_proxy_image:
            return Egress.ALLOWLIST
        return Egress.CLOSED

    # -- SandboxBackend -----------------------------------------------------------

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> _WslcSandbox:
        """Return a running container for ``key``, reusing a warm one when there is one.

        Reused, restarted and created are logged at INFO rather than left to be inferred: a
        workload returns the same output either way, and the difference between them is the
        difference between a warm exec and a fresh image start.
        """
        name = _container_name(key)
        if await self._is_listed(name, all_states=False):
            logger.info(
                "sandbox reused: container=%s kind=%s thread=%s agent=%s",
                name,
                spec.kind,
                key.thread_id,
                key.agent_dir,
            )
        elif await self._is_listed(name, all_states=True) and await self._restart(name):
            logger.info(
                "sandbox restarted: container=%s kind=%s thread=%s agent=%s",
                name,
                spec.kind,
                key.thread_id,
                key.agent_dir,
            )
        else:
            image = await self._create(name, key, spec)
            logger.info(
                "sandbox created: container=%s kind=%s image=%s thread=%s agent=%s",
                name,
                spec.kind,
                image,
                key.thread_id,
                key.agent_dir,
            )

        self._registry[(key.scope, key.thread_id, key.agent_dir)] = name
        return _WslcSandbox(self._wslc, name, self._config.command_timeout_seconds)

    async def dispose(self, key: SandboxKey) -> None:
        """Delete the container for ``key``, and in allowlist mode its proxy and network too.

        The proxy and the network are torn down after the container so the network has no
        endpoints left when it goes; a container already gone is a silent no-op.
        """
        self._registry.pop((key.scope, key.thread_id, key.agent_dir), None)
        name = _container_name(key)
        released = await self._remove(name)
        if self._config.egress_proxy_image is not None:
            await self._remove(_proxy_name(name))
            await self._remove_network(_network_name(name))
        if released:
            logger.info(
                "sandbox released: container=%s thread=%s agent=%s",
                name,
                key.thread_id,
                key.agent_dir,
            )

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        """Delete every container labelled ``(scope, thread_id)``; returns how many sandboxes.

        The labels are the source of truth, because a conversation delete has to reach
        containers this process never created.  The registry is what is left when the listing
        itself fails, and its entries are dropped either way: an entry pointing at a container
        that may already be gone is worse than no entry.

        A proxy carries the same scope labels, so it is listed and removed alongside its
        sandbox — but it is not a sandbox, so it is not counted, and its removal is what tells
        us to sweep the network named for its workload.
        """
        mine = [k for k in list(self._registry) if k[0] == scope and k[1] == thread_id]
        remembered = [self._registry.pop(k) for k in mine]

        listed = await self._scope_container_names(scope, thread_id)
        targets = [*listed, *(n for n in remembered if n not in listed)]

        count = 0
        for target in targets:
            if await self._remove(target) and not target.endswith(_PROXY_SUFFIX):
                logger.info(
                    "sandbox released: container=%s thread=%s (scope purge)", target, thread_id
                )
                count += 1
        for proxy in (t for t in targets if t.endswith(_PROXY_SUFFIX)):
            workload = proxy.removesuffix(_PROXY_SUFFIX)
            await self._remove_network(_network_name(workload))
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
        return _WslcResult(
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

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
        return result.returncode == 0 and name in _listed_names(result.stdout)

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
            result.stderr.strip(),
        )
        await self._remove(name)
        return False

    async def _create(self, name: str, key: SandboxKey, spec: SandboxSpec) -> str:
        """Create and start a container for ``key``; returns the image it ran.

        Closed mode is one command.  Allowlist mode builds the enforcing topology first —
        internal network, dual-homed proxy — so by the time the workload starts, its only
        route out already filters on the spec's hosts.
        """
        image = spec.image_id or spec.image
        if not image:
            raise ValueError(
                "No sandbox image is configured: the spec names neither image nor image_id."
            )

        args = ["container", "run", "-d", "--name", name]
        if self._config.egress_proxy_image is None:
            args += ["--network", "none"]
        else:
            await self._ensure_network(_network_name(name), key, spec)
            await self._ensure_proxy(name, key, spec, self._config.egress_proxy_image)
            proxy_url = f"http://{_proxy_name(name)}:{_PROXY_PORT}"
            args += ["--network", _network_name(name)]
            args += ["-e", f"HTTPS_PROXY={proxy_url}", "-e", f"HTTP_PROXY={proxy_url}"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["-l", f"{label}={value}"]
        args += [image, "sleep", "infinity"]

        result = await self._wslc(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0:
            if _ALREADY_EXISTS in result.stderr and await self._adopt(name):
                logger.info("container %s already existed; adopted it instead of creating", name)
                return image
            raise RuntimeError(f"wslc could not create container {name}: {result.stderr.strip()}")
        return image

    async def _ensure_network(self, net: str, key: SandboxKey, spec: SandboxSpec) -> None:
        """Create the sandbox's internal network, adopting one already there."""
        args = ["network", "create", "--internal"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["-l", f"{label}={value}"]
        args.append(net)
        result = await self._wslc(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0 and _ALREADY_EXISTS not in result.stderr:
            raise RuntimeError(f"wslc could not create network {net}: {result.stderr.strip()}")

    async def _ensure_proxy(
        self, name: str, key: SandboxKey, spec: SandboxSpec, proxy_image: str
    ) -> None:
        """Start the filtering proxy for ``name``'s sandbox, adopting one already there.

        A fresh proxy is dual-homed after starting — its second leg is what has egress — and
        then awaited until it listens, so the workload's first request cannot beat it up.  An
        adopted proxy skips both: it was connected and listening before, and its allowlist is
        the same because the same spec produced it.
        """
        proxy = _proxy_name(name)
        args = ["container", "run", "-d", "--name", proxy, "--network", _network_name(name)]
        args += ["-e", f"{_ALLOW_ENV}={','.join(spec.egress_allow)}"]
        for label, value in _sandbox_labels(key, spec).items():
            args += ["-l", f"{label}={value}"]
        args += ["-l", f"{_LABEL_ROLE}=proxy", proxy_image]

        result = await self._wslc(*args, timeout=self._config.command_timeout_seconds)
        if result.returncode != 0:
            if _ALREADY_EXISTS in result.stderr and await self._adopt(proxy):
                logger.info("egress proxy %s already existed; adopted it", proxy)
                return
            raise RuntimeError(
                f"wslc could not start the egress proxy {proxy}: {result.stderr.strip()} — "
                f"if the image is missing, build it: wslc build -t {proxy_image} "
                f"{build_context()}"
            )

        connect = await self._wslc(
            "network", "connect", "bridge", proxy, timeout=self._config.command_timeout_seconds
        )
        if connect.returncode != 0:
            # A proxy without its egress leg would turn ALLOWLIST into CLOSED silently.
            raise RuntimeError(
                f"wslc could not give the egress proxy {proxy} its outbound leg: "
                f"{connect.stderr.strip()}"
            )
        await self._await_listening(proxy)

    async def _await_listening(self, proxy: str) -> None:
        """Wait for the proxy's listening line, briefly; proceed either way, loudly if not."""
        for _ in range(_PROXY_READY_ATTEMPTS):
            result = await self._wslc(
                "container", "logs", proxy, timeout=self._config.command_timeout_seconds
            )
            if result.returncode == 0 and _PROXY_READY_MARKER in result.stdout:
                return
            await asyncio.sleep(_PROXY_READY_DELAY_S)
        logger.warning("egress proxy %s never reported listening; continuing anyway", proxy)

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
        if _NOT_FOUND not in result.stderr:
            logger.warning(
                "wslc backend: failed to remove container %s: %s", target, result.stderr.strip()
            )
        return False

    async def _scope_container_names(self, scope: str, thread_id: str) -> list[str]:
        """Container names labelled ``(scope, thread_id)``, read from wslc. Never raises.

        By name rather than id, because the proxy/network pairing is expressed in the names and
        ``container list`` does not report labels back.  ``_label_value`` on both sides, always:
        these have to be the same strings the create wrote, or the query matches nothing and
        every container for the deleted conversation keeps running — silently, since "found
        none" and "there were none" are one result.
        """
        try:
            result = await self._wslc(
                "container",
                "list",
                "-a",
                "--format",
                "json",
                "--filter",
                f"label={_LABEL_SCOPE}={_label_value(scope)}",
                "--filter",
                f"label={_LABEL_THREAD}={_label_value(thread_id)}",
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - purge must never fail
            logger.warning(
                "wslc backend: could not list containers for thread %s: %s", thread_id, exc
            )
            return []
        if result.returncode != 0:
            logger.warning(
                "wslc backend: could not list containers for thread %s: %s",
                thread_id,
                result.stderr.strip(),
            )
            return []
        return _listed_names(result.stdout)

    async def _remove_network(self, net: str) -> bool:
        """Force-remove a network. Returns whether it removed one; never raises."""
        try:
            result = await self._wslc(
                "network", "remove", net, timeout=self._config.command_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.warning("wslc backend: failed to remove network %s: %s", net, exc)
            return False
        return result.returncode == 0
