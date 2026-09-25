"""The Docker Sandboxes backend: one ``sbx`` microVM per sandbox, driven through the CLI.

Every sandbox is created with ``--deny-network "**"``, fixed CPU and memory, the shared skills
store off, no published ports, and one mount: a fresh host directory this backend owns.  That
mount is bound again at the storage base's parent, so the base is a real directory in the guest,
and stats, reads, listings and writes act on the host side of it (see ``_plane.py``).

``sbx`` has no labels, so ownership lives in the name: a prefix, then digests of the
conversation, the whole key and the kind.  The workspace directory carries the same name, and
is the second record :meth:`SbxSandboxBackend.dispose` reads, so a listing the daemon got
wrong cannot make a sandbox look absent.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import json
import logging
import os
import posixpath
import secrets
import shutil
import threading
from collections.abc import AsyncGenerator, Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from maf_sandbox import (
    BackendDeclarations,
    Capability,
    DisposalFailure,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxEntry,
    SandboxKey,
    SandboxSpec,
    ScopePurge,
    fold_disposal_failures,
)
from maf_sandbox.paths import (
    confine_resolve_guest_delete_path,
    confine_resolve_guest_list_path,
    confine_resolve_guest_path,
    confine_resolve_guest_read_path,
    confine_resolve_guest_write_path,
    ensure_guest_work_dir,
    guest_path_relative_to,
    posix_work_dir_ancestors,
    resolve_guest_working_directory,
)

from ._config import SbxSandboxConfig
from ._plane import WorkspacePlane

if TYPE_CHECKING:
    from maf_sandbox import Sandbox, SandboxBackend

__all__ = [
    "BACKEND_NAME",
    "SbxDaemonFault",
    "SbxError",
    "SbxHostNotConfined",
    "SbxLoginRequired",
    "SbxSandboxBackend",
]

logger = logging.getLogger(__name__)

BACKEND_NAME = "docker-sbx"

_CAPABILITIES = frozenset(
    {
        Capability.EXEC,
        Capability.FILES_IN,
        Capability.FILES_OUT,
        Capability.FILES_LIST,
        Capability.FILES_DELETE,
        Capability.RECLAIM,
    }
)
_DEFAULT_BASE = "/maf-sandbox/work"
_WORKSPACE = "ws"
_META = "meta.json"
_META_VERSION = 1
_PROBE_PREFIX = ".maf-sbx-probe-"
#: The instance id of a sandbox still being set up; never returned from acquire.
_SETTING_UP = "setting-up"
#: Kept at the workspace root; its absence in the guest means the bind mount is gone.
_MARKER = ".maf-sbx-workspace"

#: Runs every command.  It runs nothing while the bind mount is missing, which an auto-stop
#: causes.  argv arrives base64-encoded behind an ``x`` so no argument is empty, which ``sbx``
#: refuses.  The nonce on stderr marks where the guest's own stderr begins, and proves the
#: wrapper ran.  ``setsid`` gives the command its own process group, recorded in the pid file, so
#: a deadline can kill the whole group.
_EXEC_SCRIPT = r"""n=$1 f=$2 m=$3
shift 3
if [ ! -e "$m" ]; then printf '%s-unmounted\n' "$n" >&2; exit 1; fi
printf '%s\n' "$n" >&2
d() { printf %s "${1#x}" | base64 -d && printf x; }
w=$(d "$1") || exit 125
w=${w%x}
shift
c=$#
i=0
while [ "$i" -lt "$c" ]; do
  a=$(d "$1") || exit 125
  shift
  set -- "$@" "${a%x}"
  i=$((i + 1))
done
cd "$w" || exit 125
g='echo $$ > "$MAF_SBX_PGID_FILE" && unset MAF_SBX_PGID_FILE && exec "$@"'
MAF_SBX_PGID_FILE=$f setsid -w sh -c "$g" sh "$@"
s=$?
rm -f "$f"
exit "$s"
"""

#: Kills the process group an expired command recorded, waiting up to ``$2`` tenths of a second
#: for a wrapper that has not yet written it.  Exit 4 means none appeared, so the command may
#: still start.
_KILL_SCRIPT = r"""i=0
while [ ! -s "$1" ] && [ "$i" -lt "$2" ]; do sleep 0.1; i=$((i + 1)); done
[ -s "$1" ] || exit 4
p=$(cat "$1")
case $p in ''|*[!0-9]*) exit 3 ;; esac
kill -9 -"$p" 2>/dev/null
rm -f "$1"
exit 0
"""

#: Run as root: bind the workspace mount at the storage base's parent.  At create the parent
#: must not exist in the image, so nothing of the image's is hidden under the mount.
_MOUNT_SCRIPT = r"""if [ "$3" = create ] && { [ -e "$1" ] || [ -L "$1" ]; }; then
  echo "maf-sbx: $1 already exists in the image" >&2
  exit 3
fi
mkdir -p "$1" && mount --bind "$2" "$1"
"""
_PARENT_EXISTS = 3

#: Run once at create: checks the commands the wrapper, the deadline and removals use beyond
#: the ones this script already needs to run, then reads the host's probe file.
_PROBE_SCRIPT = r"""for c in rm sleep; do
  command -v "$c" >/dev/null || { echo "maf-sbx: the image has no $c" >&2; exit 127; }
done
exec cat "$1"
"""

_NOT_FOUND = "not found"
_ALREADY_EXISTS = "already exists"
_UNAVAILABLE = "backend unavailable"
_LOGIN_HINTS = ("sbx login", "not logged in", "log in", "unauthorized", "unauthenticated")
_STDERR_TAIL = 2000


class SbxError(RuntimeError):
    """An ``sbx`` command failed; the message carries its stderr."""


class SbxLoginRequired(SbxError):
    """The ``sbx`` login has lapsed; a person has to run ``sbx login``."""


class SbxDaemonFault(SbxError):
    """``sandboxd`` has lost its engine.  A fault to report, never a sandbox to replace."""


class SbxHostNotConfined(SbxError):
    """A host-wide ``sbx`` setting breaks the isolation this backend declares."""


@dataclass(frozen=True)
class _Result:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


def _failure(what: str, result: _Result) -> SbxError:
    detail = result.stderr_text.strip()[-_STDERR_TAIL:] or f"exit {result.returncode}"
    lowered = detail.lower()
    if _UNAVAILABLE in lowered:
        return SbxDaemonFault(
            f"{what}: sandboxd reports its backend unavailable ({detail}). Existing sandboxes "
            "are not gone; run `sbx daemon restart`."
        )
    if any(hint in lowered for hint in _LOGIN_HINTS):
        return SbxLoginRequired(f"{what}: the sbx login has lapsed ({detail}). Run `sbx login`.")
    return SbxError(f"{what}: {detail}")


def _digest(*parts: str, length: int) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:length]


def _conversation_prefix(prefix: str, scope: str, thread_id: str) -> str:
    return f"{prefix}-{_digest(scope, thread_id, length=12)}-"


def _key_prefix(prefix: str, key: SandboxKey) -> str:
    whole = _digest(key.scope, key.thread_id, key.agent_id, key.call_id, length=10)
    return f"{_conversation_prefix(prefix, key.scope, key.thread_id)}{whole}-"


def _key_record(key: SandboxKey) -> list[str]:
    return [key.scope, key.thread_id, key.agent_id, key.call_id]


def sandbox_name(prefix: str, key: SandboxKey, kind: str) -> str:
    """The sandbox's name, which is also its workspace directory's; at most 50 characters."""
    return f"{_key_prefix(prefix, key)}{_digest(kind, length=8)}"


def _storage_base(spec: SandboxSpec) -> str:
    base = spec.work_dir if spec.work_dir is not None else _DEFAULT_BASE
    posix_work_dir_ancestors(base)
    # normpath keeps a leading `//`, which the ancestor walk spells `/`; Linux reads both as `/`.
    base = "/" + posixpath.normpath(base).lstrip("/")
    if posixpath.dirname(base) == "/":
        raise ValueError(
            f"work_dir {base!r} sits directly under '/'; this backend mounts the workspace at "
            "the base's parent, so the base needs a parent of its own"
        )
    return base


def _encode(value: str) -> str:
    if "\0" in value:
        raise ValueError("an argument contains a NUL byte, which no argv can carry")
    return "x" + base64.b64encode(value.encode("utf-8", errors="surrogateescape")).decode()


@dataclass(frozen=True)
class _Mount:
    """Where the workspace appears in the guest, and where the backend binds it again."""

    guest_mount: str
    parent: str
    host: Path


def _guest_stderr(stderr: bytes, nonce: str) -> bytes | None:
    marker = f"{nonce}\n".encode()
    at = stderr.find(marker)
    return None if at < 0 else stderr[at + len(marker) :]


class _SbxSandbox:
    """One acquired sandbox: ``exec`` through the wrapper, files through the host plane."""

    def __init__(
        self,
        backend: SbxSandboxBackend,
        name: str,
        instance_id: str,
        base: str,
        plane: WorkspacePlane,
        mount: _Mount,
    ) -> None:
        self._backend = backend
        self._name = name
        self._instance_id = instance_id
        self._base = base
        self._plane = plane
        self._mount = mount

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def base(self) -> str:
        return self._base

    @property
    def plane(self) -> WorkspacePlane:
        return self._plane

    @property
    def mount(self) -> _Mount:
        return self._mount

    def _cwd(self, working_directory: str) -> str:
        return resolve_guest_working_directory(working_directory, self._base)

    async def _stat(self, guest: str) -> SandboxEntry | None:
        return await asyncio.to_thread(self._plane.lstat, guest)

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        cwd = self._cwd(working_directory)
        argv = ["sh", "-c", command] if isinstance(command, str) else list(command)
        if not argv:
            raise ValueError("an argv sequence needs at least one element")
        return await self._backend.run_in_guest(
            self._name, argv, cwd=cwd, timeout=timeout, mount=self._mount
        )

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        raise NotImplementedError(
            f"{BACKEND_NAME} does not declare RUN_CODE: any image is accepted, so the runtime "
            "is the image's"
        )

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        guest = await confine_resolve_guest_write_path(
            self._stat, path, self._cwd(working_directory)
        )
        await asyncio.to_thread(self._plane.write, guest, data)

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        cwd = self._cwd(working_directory)
        guest = await confine_resolve_guest_read_path(self._stat, path, cwd)
        entry = await self._stat(guest)
        if entry is None:
            return None
        relative = guest_path_relative_to(guest, cwd)
        return SandboxEntry(path=relative or ".", kind=entry.kind, size_bytes=entry.size_bytes)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        guest = await confine_resolve_guest_read_path(
            self._stat, path, self._cwd(working_directory)
        )
        return await asyncio.to_thread(self._plane.read, guest, max_bytes)

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        cwd = self._cwd(working_directory)
        guest = await confine_resolve_guest_list_path(self._stat, path, cwd)
        listed = await asyncio.to_thread(self._plane.list, guest)
        entries: list[SandboxEntry] = []
        for name, entry in listed:
            relative = guest_path_relative_to(posixpath.join(guest, name), cwd)
            if relative is None:
                continue
            entries.append(SandboxEntry(relative, entry.kind, entry.size_bytes))
        return tuple(entries)

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        guest = await confine_resolve_guest_delete_path(
            self._stat, path, self._cwd(working_directory)
        )
        entry = await self._stat(guest)
        if entry is None:
            return
        if entry.kind is EntryKind.DIRECTORY and not recursive:
            raise IsADirectoryError(errno.EISDIR, "a directory needs recursive=True", guest)
        await self._remove_as_the_guest(
            guest, self._backend.config.command_timeout_seconds, recursive=recursive
        )

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        cwd = self._cwd(working_directory)
        if posixpath.isabs(directory):
            guest = posixpath.normpath(directory)
        else:
            guest = confine_resolve_guest_path(directory, cwd)
            if guest == posixpath.normpath(cwd):
                raise ValueError(f"refusing to reclaim the working directory itself: {guest!r}")
        if self._plane.parts(guest) in (None, ()):
            raise ValueError(f"refusing to reclaim {guest!r}, which is not inside the workspace")
        # The guest's lookups fold case as the host's do, so `upper` would remove `Upper`.
        await asyncio.wait_for(self._stat(guest), max(0.0, deadline - loop.time()))
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError(f"reclaiming {guest!r} did not finish within {timeout} seconds")
        await self._remove_as_the_guest(guest, left, recursive=True)

    async def _remove_as_the_guest(self, guest: str, timeout: float, *, recursive: bool) -> None:
        # In the guest rather than on the host: a guest that looked a name up keeps seeing it
        # for seconds after the host deletes it.  The guest's authority reaches only its own
        # VM and this workspace, so a swapped component redirects nothing it could not delete.
        result = await self._backend.run_in_guest(
            # `-f` alone refuses a directory, so one swapped in after the host's stat survives.
            self._name,
            ["rm", "-rf" if recursive else "-f", "--", guest],
            cwd="/",
            timeout=timeout,
            mount=self._mount,
        )
        if result.exit_code != 0:
            raise OSError(
                errno.EACCES, f"the guest could not remove it: {result.stderr.strip()}", guest
            )

    async def reset(self, *, timeout: float) -> None:
        raise NotImplementedError(f"{BACKEND_NAME} does not declare SNAPSHOT")


class SbxSandboxBackend:
    """Hands out Docker Sandboxes microVMs through the ``sbx`` CLI, one per ``(key, kind)``."""

    def __init__(self, config: SbxSandboxConfig | None = None) -> None:
        self._config = config if config is not None else SbxSandboxConfig()
        self._declarations = BackendDeclarations(
            capabilities=_CAPABILITIES,
            egress_modes=frozenset({Egress.CLOSED}),
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset({IsolationScope.CONVERSATION}),
            observes_egress=False,
        )
        # Per loop, because an asyncio.Lock binds to the loop that first waits on it; counted, so
        # the last caller out drops the entry and the table holds only names in use.
        self._locks: dict[tuple[int, str], tuple[asyncio.Lock, int]] = {}
        self._locks_guard = threading.Lock()

    @property
    def name(self) -> str:
        return BACKEND_NAME

    @property
    def isolation(self) -> Isolation:
        return Isolation.MICROVM

    @property
    def declarations(self) -> BackendDeclarations:
        return self._declarations

    @property
    def config(self) -> SbxSandboxConfig:
        return self._config

    # --- the CLI ------------------------------------------------------------------------

    async def _sbx(self, *args: str, timeout: float | None = None) -> _Result:
        """Run one ``sbx`` command; a timeout kills the client and raises ``TimeoutError``."""
        process = await asyncio.create_subprocess_exec(
            self._config.sbx_path,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout if timeout is not None else self._config.command_timeout_seconds,
            )
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.shield(process.wait())
            raise
        return _Result(cast(int, process.returncode), stdout, stderr)

    async def run_in_guest(
        self, name: str, argv: Sequence[str], *, cwd: str, timeout: float, mount: _Mount
    ) -> ExecResult:
        """Run ``argv`` in ``cwd`` under the wrapper, killing its process group at ``timeout``.

        ``timeout`` covers a re-mount after an auto-stop as well as the command.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        encoded = (_encode(cwd), *(_encode(arg) for arg in argv))
        marker = posixpath.join(mount.parent, _MARKER)
        expired = f"the command did not finish within {timeout} seconds"
        for attempt in range(2):
            nonce = secrets.token_hex(12)
            pid_file = f"/tmp/maf-sbx-{nonce}.pgid"
            args = ("exec", name, "sh", "-c", _EXEC_SCRIPT, "maf-sbx", nonce, pid_file, marker)
            left = deadline - loop.time()
            if left <= 0:
                raise TimeoutError(expired)
            try:
                result = await self._sbx(*args, *encoded, timeout=left)
            except (TimeoutError, asyncio.CancelledError) as stopped:
                # Killing the client leaves the guest's command running in a reusable sandbox,
                # and a cancellation must not stop the kill either.
                await asyncio.shield(self._stop_the_command(name, pid_file))
                if isinstance(stopped, TimeoutError):
                    raise TimeoutError(expired) from None
                raise
            stderr = _guest_stderr(result.stderr, nonce)
            if stderr is not None:
                # The command started; what follows the nonce is its own, whatever it says.
                return ExecResult(
                    stdout_bytes=result.stdout, stderr_bytes=stderr, exit_code=result.returncode
                )
            if f"{nonce}-unmounted\n".encode() not in result.stderr:
                raise _failure(f"sbx exec in {name}", result)
            if attempt:
                break
            left = deadline - loop.time()
            if left <= 0:
                raise TimeoutError(expired)
            try:
                await self._bind_workspace(name, mount, create=False, timeout=left)
            except TimeoutError:
                raise TimeoutError(expired) from None
        raise SbxError(f"the workspace is still not mounted at {mount.parent!r} in {name}")

    async def _bind_workspace(
        self, name: str, mount: _Mount, *, create: bool, timeout: float | None = None
    ) -> None:
        bound = await self._sbx(
            "exec",
            "-u",
            "root",
            name,
            "sh",
            "-c",
            _MOUNT_SCRIPT,
            "maf-sbx",
            mount.parent,
            mount.guest_mount,
            "create" if create else "again",
            timeout=timeout,
        )
        if bound.returncode == _PARENT_EXISTS and create:
            raise ValueError(
                f"{mount.parent!r} already exists in the image; this backend mounts the "
                "workspace there, so choose a work_dir whose parent the image lacks"
            )
        if bound.returncode != 0:
            raise _failure(f"mounting the workspace at {mount.parent}", bound)
        # The guest can plant a link at the marker; the plane replaces the name, never its target.
        plane = WorkspacePlane(mount.host, mount.parent)
        await asyncio.to_thread(plane.write, posixpath.join(mount.parent, _MARKER), b"")

    async def _stop_the_command(self, name: str, pid_file: str) -> None:
        """Kill an expired command's process group, or stop the sandbox when that cannot."""
        if await self._kill_group(name, pid_file):
            return
        # Stopping the sandbox kills every process in it and keeps its files; the next command
        # starts it again.
        try:
            stopped = await self._sbx("stop", name)
        except TimeoutError:
            logger.warning("docker-sbx: stopping %s after an expired command timed out", name)
            return
        if stopped.returncode != 0:
            logger.warning(
                "docker-sbx: could not stop %s after an expired command: %s",
                name,
                stopped.stderr_text.strip()[-_STDERR_TAIL:],
            )

    async def _kill_group(self, name: str, pid_file: str) -> bool:
        """Whether the expired command's process group is known to be killed."""
        allowance = self._config.exec_cleanup_timeout_seconds
        tenths = max(1, int((allowance - 1) * 10))
        try:
            result = await self._sbx(
                "exec",
                name,
                "sh",
                "-c",
                _KILL_SCRIPT,
                "maf-sbx",
                pid_file,
                str(tenths),
                timeout=allowance,
            )
        except TimeoutError:
            logger.warning("docker-sbx: killing an expired command in %s timed out", name)
            return False
        if result.returncode != 0:
            logger.warning(
                "docker-sbx: could not kill an expired command in %s (exit %s): %s",
                name,
                result.returncode,
                result.stderr_text.strip()[-_STDERR_TAIL:],
            )
            return False
        return True

    async def _listing(self) -> dict[str, dict[str, object]]:
        result = await self._sbx("ls", "--json")
        if result.returncode != 0:
            raise _failure("sbx ls", result)
        payload = json.loads(result.stdout or b"{}")
        rows = cast("list[dict[str, object]]", payload.get("sandboxes") or [])
        return {str(row["name"]): row for row in rows if "name" in row}

    async def check_host(self) -> None:
        """Refuse unless the host-wide settings this backend's isolation rests on hold."""
        forwarding, servers = await asyncio.gather(
            self._sbx("settings", "get", "ssh.agentForwardingEnabled"),
            self._sbx("mcp", "ls", "--json"),
        )
        if forwarding.returncode != 0:
            raise _failure("sbx settings get ssh.agentForwardingEnabled", forwarding)
        if forwarding.stdout.decode(errors="replace").strip().lower() != "false":
            raise SbxHostNotConfined(
                "SSH agent forwarding is on, so every sandbox gets a socket to the host's SSH "
                "agent. Run `sbx settings set ssh.agentForwardingEnabled false`, then "
                "`sbx daemon restart`."
            )
        if servers.returncode != 0:
            raise _failure("sbx mcp ls", servers)
        registered = cast("list[object]", json.loads(servers.stdout or b"{}").get("servers") or [])
        if registered:
            raise SbxHostNotConfined(
                f"{len(registered)} MCP server(s) are registered with sbx, and the MCP gateway "
                "answers a sandbox even under deny-all. Remove them with `sbx mcp rm`."
            )

    # --- ownership ----------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _locked(self, name: str) -> AsyncGenerator[None]:
        key = (id(asyncio.get_running_loop()), name)
        with self._locks_guard:
            held = self._locks.get(key)
            lock = held[0] if held is not None else asyncio.Lock()
            self._locks[key] = (lock, (held[1] if held is not None else 0) + 1)
        try:
            async with lock:
                yield
        finally:
            with self._locks_guard:
                _, callers = self._locks[key]
                if callers > 1:
                    self._locks[key] = (lock, callers - 1)
                else:
                    del self._locks[key]

    def _directory(self, name: str) -> Path:
        return self._config.resolved_workspace_root / name

    def _plane(self, name: str, base: str) -> WorkspacePlane:
        return WorkspacePlane(self._directory(name) / _WORKSPACE, posixpath.dirname(base))

    def _read_meta(self, name: str) -> dict[str, object] | None:
        try:
            return json.loads((self._directory(name) / _META).read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    async def _run_setup(self, what: str, *args: str) -> _Result:
        result = await self._sbx(*args)
        if result.returncode != 0:
            raise _failure(what, result)
        return result

    # --- acquire ------------------------------------------------------------------------

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> _SbxSandbox:
        if spec.egress is not Egress.CLOSED:
            raise ValueError(f"{BACKEND_NAME} enforces only Egress.CLOSED")
        base = _storage_base(spec)
        name = sandbox_name(self._config.name_prefix, key, spec.kind)
        await self.check_host()
        async with self._locked(name):
            row = (await self._listing()).get(name)
            if row is None and self._read_meta(name) is not None:
                await self._confirm_absent(name)
            if row is None:
                sandbox, created = await self._create(name, key, spec, base)
            else:
                sandbox, created = self._adopt(key, name, row, spec, base), False
            try:
                await ensure_guest_work_dir(
                    spec,
                    sandbox._stat,  # pyright: ignore[reportPrivateUsage]
                    lambda missing: asyncio.to_thread(sandbox.plane.make_directories, missing[-1]),
                    resolve=posix_work_dir_ancestors,
                    base=base,
                )
            except BaseException:
                if created:
                    await self._discard(name)
                raise
            return sandbox

    def _adopt(
        self, key: SandboxKey, name: str, row: dict[str, object], spec: SandboxSpec, base: str
    ) -> _SbxSandbox:
        meta = self._read_meta(name)
        workspace = str(self._directory(name) / _WORKSPACE)
        if meta is None:
            raise SbxError(
                f"sandbox {name} has no record in {self._directory(name)}; it was created by "
                "another backend or its create was interrupted. Dispose it."
            )
        if row.get("workspaces") != [workspace]:
            raise SbxError(
                f"sandbox {name} exists but its workspace is not {workspace}; it was created "
                "with a different workspace_root. Dispose it or use the same root."
            )
        if meta.get("instance_id") != row.get("id"):
            raise SbxError(
                f"sandbox {name}'s record describes another instance; its create did not "
                "finish, or it was replaced outside this backend. Dispose it."
            )
        if meta.get("key") != _key_record(key) or meta.get("kind") != spec.kind:
            raise SbxError(
                f"sandbox {name} was created for another key or kind, whose name digests to "
                "the same; refusing to share it. Dispose it."
            )
        if (
            meta.get("work_dir") != base
            or meta.get("image") != spec.image
            or meta.get("image_id") != spec.image_id
        ):
            raise ValueError(
                f"sandbox {name} was created with work_dir {meta.get('work_dir')!r}, image "
                f"{meta.get('image')!r} and image_id {meta.get('image_id')!r}; dispose it "
                "before changing any of them"
            )
        guest_mount = meta.get("guest_mount")
        if not isinstance(guest_mount, str):
            raise SbxError(f"sandbox {name}'s record does not say where its workspace is mounted")
        return self._sandbox(name, row, base, guest_mount)

    def _sandbox(
        self, name: str, row: dict[str, object], base: str, guest_mount: str
    ) -> _SbxSandbox:
        instance_id = row.get("id")
        if not isinstance(instance_id, str) or not instance_id:
            raise SbxError(
                f"`sbx ls` gives sandbox {name} no id, so it could not be disposed exactly; the "
                "daemon may have lost its engine. Run `sbx daemon restart`."
            )
        mount = _Mount(guest_mount, posixpath.dirname(base), self._directory(name) / _WORKSPACE)
        return _SbxSandbox(self, name, instance_id, base, self._plane(name, base), mount)

    async def _create(
        self, name: str, key: SandboxKey, spec: SandboxSpec, base: str
    ) -> tuple[_SbxSandbox, bool]:
        """The sandbox, and whether this call created it rather than adopting a winner's."""
        directory = self._directory(name)
        workspace = directory / _WORKSPACE
        root = self._config.resolved_workspace_root
        await asyncio.to_thread(_make_private, root, directory, workspace)
        meta: dict[str, object] = {
            "version": _META_VERSION,
            "key": _key_record(key),
            "kind": spec.kind,
            "work_dir": base,
            "image": spec.image,
            "image_id": spec.image_id,
        }
        args = [
            "create",
            "shell",
            "--name",
            name,
            "--cpus",
            str(self._config.cpus),
            "--memory",
            self._config.memory,
            "--skills",
            "off",
            "--deny-network",
            "**",
            "--quiet",
        ]
        template = spec.image_id or spec.image
        if template:
            args += ["--template", template]
        try:
            created = await self._sbx(
                *args, str(workspace), timeout=self._config.create_timeout_seconds
            )
        except BaseException:
            # The daemon may finish a create whose client was stopped. Only a record naming the
            # listed instance means another process finished it; an older one is stale.
            if not await self._finished_by_another(name):
                await self._discard(name)
            raise
        if created.returncode != 0:
            if _ALREADY_EXISTS in created.stderr_text:
                return await self._adopt_the_winner(key, name, spec, base), False
            refusal = _failure(f"sbx create {name}", created)
            await self._abandon_workspace(directory, refusal)
            raise refusal
        try:
            # The create proved nothing held this name, so anything in the workspace is stale.
            await asyncio.to_thread(_empty, workspace)
            mounted = await self._run_setup(
                "reading the workspace mount", "exec", name, "sh", "-c", "pwd -P"
            )
            guest_mount = mounted.stdout.decode("utf-8", errors="replace").strip()
            if not guest_mount.startswith("/") or "\n" in guest_mount:
                raise SbxError(f"the guest reported {guest_mount!r} as its workspace mount")
            meta["guest_mount"] = guest_mount
            sandbox = self._sandbox(name, {"id": _SETTING_UP}, base, guest_mount)
            await self._bind_workspace(name, sandbox.mount, create=True)
            await self._prove_the_mount(sandbox)
            row = (await self._listing()).get(name) or {}
            served = self._sandbox(name, row, base, guest_mount)
            # Last, so a record another process can read means the sandbox is ready to serve.
            meta["instance_id"] = served.instance_id
            await asyncio.to_thread(_write_record, directory / _META, meta)
        except BaseException:
            await self._discard(name)
            raise
        return served, True

    async def _finished_by_another(self, name: str) -> bool:
        try:
            row = (await self._listing()).get(name)
        except (SbxError, TimeoutError, ValueError):
            return False
        meta = self._read_meta(name)
        return row is not None and meta is not None and meta.get("instance_id") == row.get("id")

    async def _confirm_absent(self, name: str) -> None:
        """Ask a sandbox the listing omits whether it is there, since a lost engine lists none."""
        answered = await self._sbx("exec", name, "sh", "-c", ":")
        if answered.returncode == 0:
            raise SbxDaemonFault(
                f"sandbox {name} answers although `sbx ls` does not list it; the daemon may have "
                "lost its engine. Run `sbx daemon restart`."
            )
        if _NOT_FOUND not in answered.stderr_text:
            raise _failure(f"checking whether sandbox {name} is there", answered)

    async def _abandon_workspace(self, directory: Path, refusal: SbxError) -> None:
        """Remove the empty directories a refused create made; never content, never on a fault.

        A listed name keeps them: another process's sandbox mounts that workspace, and is empty
        between its create and its bind.
        """
        if isinstance(refusal, SbxDaemonFault):
            return
        try:
            if directory.name in await self._listing():
                return
        except (SbxError, TimeoutError, ValueError):
            return
        for path in (directory / _WORKSPACE, directory):
            with contextlib.suppress(OSError):
                await asyncio.to_thread(path.rmdir)

    async def _adopt_the_winner(
        self, key: SandboxKey, name: str, spec: SandboxSpec, base: str
    ) -> _SbxSandbox:
        """Serve the sandbox a concurrent create made, which the conflict proved exists."""
        row = (await self._listing()).get(name)
        if row is None:
            raise SbxDaemonFault(
                f"sandbox {name} already exists although `sbx ls` does not list it; the daemon "
                "may have lost its engine. Run `sbx daemon restart`."
            )
        meta = self._read_meta(name)
        if meta is None or meta.get("instance_id") != row.get("id"):
            raise SbxError(
                f"sandbox {name} is being created by another process; acquire again once it is up"
            )
        return self._adopt(key, name, row, spec, base)

    async def _prove_the_mount(self, sandbox: _SbxSandbox) -> None:
        """Have the guest read, through the mount and the wrapper, a file the host just wrote."""
        mount = sandbox.mount
        probe_name = f"{_PROBE_PREFIX}{secrets.token_hex(8)}"
        token = secrets.token_hex(16)
        await asyncio.to_thread((mount.host / probe_name).write_text, token, "ascii")
        try:
            probe = await sandbox.exec(
                ["sh", "-c", _PROBE_SCRIPT, "maf-sbx", posixpath.join(mount.parent, probe_name)],
                working_directory="/",
                timeout=self._config.command_timeout_seconds,
            )
        finally:
            await asyncio.to_thread((mount.host / probe_name).unlink, True)
        if probe.exit_code != 0 or probe.stdout != token:
            raise SbxError(
                f"the guest could not read the workspace at {mount.parent!r}; the image needs sh, "
                "base64, setsid, mount, mkdir, cat, rm and sleep for this backend: "
                f"{probe.stderr.strip()}"
            )

    async def _discard(self, name: str) -> None:
        failure = await asyncio.shield(self._remove(name, None))
        if failure is not None:
            logger.warning(
                "docker-sbx: could not remove %s after a failed acquire: %s", name, failure
            )

    # --- disposal -----------------------------------------------------------------------

    async def _remove(self, name: str, instance_id: str | None) -> DisposalFailure | None:
        """Delete one sandbox and its workspace; absence is success."""
        if instance_id is not None:
            try:
                row = (await self._listing()).get(name)
            except (SbxError, TimeoutError, ValueError) as error:
                return DisposalFailure("unlisted", str(error))
            if row is None and await asyncio.to_thread(self._directory(name).exists):
                # An engine that has lost its sandboxes lists none; the workspace says otherwise.
                return DisposalFailure(
                    "unlisted",
                    f"`sbx ls` does not show {name} but its workspace remains; the daemon may "
                    "have lost its engine. Run `sbx daemon restart`.",
                )
            if row is None or row.get("id") != instance_id:
                return None
        try:
            removed = await self._sbx("rm", "--force", name)
        except TimeoutError:
            return DisposalFailure("timeout", f"sbx rm {name} timed out")
        if removed.returncode != 0 and _NOT_FOUND not in removed.stderr_text:
            error = _failure(f"sbx rm {name}", removed)
            code = "unreachable" if isinstance(error, SbxDaemonFault) else "refused"
            return DisposalFailure(code, str(error))
        try:
            await asyncio.to_thread(_delete_tree, self._directory(name))
        except OSError as error:
            return DisposalFailure("unknown", f"removing {name}'s workspace: {error}")
        return None

    async def _sweep(
        self, prefix: str, instance_id: str | None = None
    ) -> tuple[int, list[DisposalFailure]]:
        failures: list[DisposalFailure] = []
        names: set[str] = set()
        try:
            names |= {name for name in await self._listing() if name.startswith(prefix)}
        except (SbxError, TimeoutError, ValueError) as error:
            failures.append(DisposalFailure("unlisted", str(error)))
        names |= await asyncio.to_thread(
            _directories_named, self._config.resolved_workspace_root, prefix
        )
        disposed = 0
        for name in sorted(names):
            async with self._locked(name):
                failure = await self._remove(name, instance_id)
            if failure is None:
                disposed += 1
            else:
                failures.append(failure)
        return disposed, failures

    async def dispose(
        self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
    ) -> DisposalFailure | None:
        prefix = (
            sandbox_name(self._config.name_prefix, key, kind)
            if kind is not None
            else _key_prefix(self._config.name_prefix, key)
        )
        _, failures = await _never_raises(self._sweep(prefix, instance_id))
        return fold_disposal_failures(failures)

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        prefix = _conversation_prefix(self._config.name_prefix, scope, thread_id)
        disposed, failures = await _never_raises(self._sweep(prefix))
        return ScopePurge(disposed=disposed, undisposed=fold_disposal_failures(failures))


async def _never_raises(
    sweep: Awaitable[tuple[int, list[DisposalFailure]]],
) -> tuple[int, list[DisposalFailure]]:
    try:
        return await sweep
    except Exception as error:  # noqa: BLE001 - disposal reports rather than raises
        return 0, [DisposalFailure("unknown", f"{type(error).__name__}: {error}")]


def _make_private(root: Path, directory: Path, workspace: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for path in (directory, workspace):
        path.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(root, 0o700)


def _empty(workspace: Path) -> None:
    for child in workspace.iterdir():
        if child.is_dir() and not child.is_symlink() and not child.is_junction():
            shutil.rmtree(child)
        else:
            child.unlink()


def _write_record(path: Path, meta: dict[str, object]) -> None:
    part = path.with_name(f".{path.name}.{secrets.token_hex(8)}.part")
    part.write_text(json.dumps(meta), "utf-8")
    os.replace(part, path)


def _delete_tree(directory: Path) -> None:
    if directory.exists() or directory.is_symlink():
        shutil.rmtree(directory)


def _directories_named(root: Path, prefix: str) -> set[str]:
    try:
        return {entry.name for entry in root.iterdir() if entry.name.startswith(prefix)}
    except FileNotFoundError:
        return set()


# Checks the full protocol signatures, which `runtime_checkable` does not.
if TYPE_CHECKING:
    _: tuple[SandboxBackend, type[Sandbox]] = (SbxSandboxBackend(), _SbxSandbox)
