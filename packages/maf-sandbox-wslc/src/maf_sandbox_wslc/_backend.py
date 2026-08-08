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
_ALREADY_EXISTS = "ERROR_ALREADY_EXISTS"


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
        # True because `_create` passes `--network none` and nothing can widen it.
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
        """Delete the container for ``key``. A container already gone is a silent no-op."""
        self._registry.pop((key.scope, key.thread_id, key.agent_dir), None)
        name = _container_name(key)
        if await self._remove(name):
            logger.info(
                "sandbox released: container=%s thread=%s agent=%s",
                name,
                key.thread_id,
                key.agent_dir,
            )

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        """Delete every container labelled ``(scope, thread_id)``; returns how many.

        The labels are the source of truth, because a conversation delete has to reach
        containers this process never created.  The registry is what is left when the listing
        itself fails, and its entries are dropped either way: an entry pointing at a container
        that may already be gone is worse than no entry.
        """
        mine = [k for k in list(self._registry) if k[0] == scope and k[1] == thread_id]
        remembered = [self._registry.pop(k) for k in mine]

        count = 0
        for target in [*await self._scope_container_ids(scope, thread_id), *remembered]:
            if await self._remove(target):
                logger.info(
                    "sandbox released: container=%s thread=%s (scope purge)", target, thread_id
                )
                count += 1
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
        """Create and start a closed container for ``key``; returns the image it ran."""
        image = spec.image_id or spec.image
        if not image:
            raise ValueError(
                "No sandbox image is configured: the spec names neither image nor image_id."
            )

        args = ["container", "run", "-d", "--name", name, "--network", "none"]
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

    async def _scope_container_ids(self, scope: str, thread_id: str) -> list[str]:
        """Container ids labelled ``(scope, thread_id)``, read from wslc. Never raises.

        ``_label_value`` on both sides, always: these have to be the same strings the create
        wrote, or the query matches nothing and every container for the deleted conversation
        keeps running — silently, since "found none" and "there were none" are one result.
        """
        try:
            result = await self._wslc(
                "container",
                "list",
                "-a",
                "-q",
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
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
