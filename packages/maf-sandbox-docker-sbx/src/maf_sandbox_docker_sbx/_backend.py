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

#: Kills the process group an expired command recorded, waiting briefly for a wrapper that had
#: not yet written it.
_KILL_SCRIPT = r"""i=0
while [ ! -s "$1" ] && [ "$i" -lt 20 ]; do sleep 0.1; i=$((i + 1)); done
[ -s "$1" ] || exit 0
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


def sandbox_name(prefix: str, key: SandboxKey, kind: str) -> str:
    """The sandbox's name, which is also its workspace directory's; at most 50 characters."""
    return f"{_key_prefix(prefix, key)}{_digest(kind, length=8)}"


def _storage_base(spec: SandboxSpec) -> str:
    base = spec.work_dir if spec.work_dir is not None else _DEFAULT_BASE
    posix_work_dir_ancestors(base)
    base = posixpath.normpath(base)
    if posixpath.dirname(base) == "/":
        raise ValueError(
            f"work_dir {base!r} sits directly under '/'; this backend links the base's parent "
            "to the workspace, so the base needs a parent of its own"
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
        await self._remove_as_the_guest(guest, self._backend.config.command_timeout_seconds)

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        cwd = self._cwd(working_directory)
        if posixpath.isabs(directory):
            guest = posixpath.normpath(directory)
        else:
            guest = confine_resolve_guest_path(directory, cwd)
            if guest == posixpath.normpath(cwd):
                raise ValueError(f"refusing to reclaim the working directory itself: {guest!r}")
        if self._plane.parts(guest) in (None, ()):
            raise ValueError(f"refusing to reclaim {guest!r}, which is not inside the workspace")
        await self._remove_as_the_guest(guest, timeout)

    async def _remove_as_the_guest(self, guest: str, timeout: float) -> None:
        # In the guest rather than on the host: a guest that looked a name up keeps seeing it
        # for seconds after the host deletes it.  The guest's authority reaches only its own
        # VM and this workspace, so a swapped component redirects nothing it could not delete.
        result = await self._backend.run_in_guest(
            self._name, ["rm", "-rf", "--", guest], cwd="/", timeout=timeout, mount=self._mount
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
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
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
        for attempt in range(2):
            nonce = secrets.token_hex(12)
            pid_file = f"/tmp/maf-sbx-{nonce}.pgid"
            args = ("exec", name, "sh", "-c", _EXEC_SCRIPT, "maf-sbx", nonce, pid_file, marker)
            try:
                left = deadline - loop.time()
                if left <= 0:
                    raise TimeoutError
                result = await self._sbx(*args, *encoded, timeout=left)
            except TimeoutError:
                await self._kill_group(name, pid_file)
                raise TimeoutError(f"the command did not finish within {timeout} seconds") from None
            if f"{nonce}-unmounted\n".encode() in result.stderr:
                if attempt:
                    break
                await self._bind_workspace(name, mount, create=False)
                continue
            stderr = _guest_stderr(result.stderr, nonce)
            if stderr is None:
                raise _failure(f"sbx exec in {name}", result)
            return ExecResult(
                stdout_bytes=result.stdout, stderr_bytes=stderr, exit_code=result.returncode
            )
        raise SbxError(f"the workspace is still not mounted at {mount.parent!r} in {name}")

    async def _bind_workspace(self, name: str, mount: _Mount, *, create: bool) -> None:
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
        )
        if bound.returncode == _PARENT_EXISTS and create:
            raise ValueError(
                f"{mount.parent!r} already exists in the image; this backend mounts the "
                "workspace there, so choose a work_dir whose parent the image lacks"
            )
        if bound.returncode != 0:
            raise _failure(f"mounting the workspace at {mount.parent}", bound)
        await asyncio.to_thread((mount.host / _MARKER).write_bytes, b"")

    async def _kill_group(self, name: str, pid_file: str) -> None:
        try:
            result = await self._sbx(
                "exec",
                name,
                "sh",
                "-c",
                _KILL_SCRIPT,
                "maf-sbx",
                pid_file,
                timeout=self._config.exec_cleanup_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("docker-sbx: killing an expired command in %s timed out", name)
            return
        if result.returncode != 0:
            logger.warning(
                "docker-sbx: could not kill an expired command in %s: %s",
                name,
                result.stderr_text.strip()[-_STDERR_TAIL:],
            )

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
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

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
            if row is None:
                sandbox = await self._create(name, key, spec, base)
            else:
                sandbox = self._adopt(name, row, spec, base)
            try:
                await ensure_guest_work_dir(
                    spec,
                    sandbox._stat,  # pyright: ignore[reportPrivateUsage]
                    lambda missing: asyncio.to_thread(sandbox.plane.make_directories, missing[-1]),
                    resolve=posix_work_dir_ancestors,
                    base=base,
                )
            except BaseException:
                if row is None:
                    await self._discard(name)
                raise
            return sandbox

    def _adopt(
        self, name: str, row: dict[str, object], spec: SandboxSpec, base: str
    ) -> _SbxSandbox:
        meta = self._read_meta(name)
        workspace = str(self._directory(name) / _WORKSPACE)
        if meta is None or row.get("workspaces") != [workspace]:
            raise SbxError(
                f"sandbox {name} exists but its workspace is not {workspace}; it was created "
                "with a different workspace_root. Dispose it or use the same root."
            )
        if meta.get("work_dir") != base or meta.get("image") != spec.image:
            raise ValueError(
                f"sandbox {name} was created with work_dir {meta.get('work_dir')!r} and image "
                f"{meta.get('image')!r}; dispose it before changing either"
            )
        guest_mount = meta.get("guest_mount")
        if not isinstance(guest_mount, str):
            raise SbxError(f"sandbox {name}'s record does not say where its workspace is mounted")
        return self._sandbox(name, row, base, guest_mount)

    def _sandbox(
        self, name: str, row: dict[str, object], base: str, guest_mount: str
    ) -> _SbxSandbox:
        mount = _Mount(guest_mount, posixpath.dirname(base), self._directory(name) / _WORKSPACE)
        return _SbxSandbox(
            self, name, str(row.get("id") or name), base, self._plane(name, base), mount
        )

    async def _create(
        self, name: str, key: SandboxKey, spec: SandboxSpec, base: str
    ) -> _SbxSandbox:
        directory = self._directory(name)
        workspace = directory / _WORKSPACE
        root = self._config.resolved_workspace_root
        await asyncio.to_thread(_make_private, root, directory, workspace)
        meta = {
            "version": _META_VERSION,
            "key": [key.scope, key.thread_id, key.agent_id, key.call_id],
            "kind": spec.kind,
            "work_dir": base,
            "image": spec.image,
        }
        await asyncio.to_thread((directory / _META).write_text, json.dumps(meta), "utf-8")
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
        if spec.image:
            args += ["--template", spec.image]
        created = await self._sbx(
            *args, str(workspace), timeout=self._config.create_timeout_seconds
        )
        if created.returncode != 0:
            if _ALREADY_EXISTS in created.stderr_text:
                raise SbxDaemonFault(
                    f"sandbox {name} already exists although `sbx ls` did not list it; the "
                    "daemon may have lost its engine. Run `sbx daemon restart`."
                )
            raise _failure(f"sbx create {name}", created)
        try:
            # The create proved nothing held this name, so anything in the workspace is stale.
            await asyncio.to_thread(_empty, workspace)
            mounted = await self._run_setup("reading the workspace mount", "exec", name, "pwd")
            guest_mount = mounted.stdout.decode("utf-8", errors="replace").strip()
            if not guest_mount.startswith("/") or "\n" in guest_mount:
                raise SbxError(f"the guest reported {guest_mount!r} as its workspace mount")
            meta["guest_mount"] = guest_mount
            await asyncio.to_thread((directory / _META).write_text, json.dumps(meta), "utf-8")
            sandbox = self._sandbox(name, {}, base, guest_mount)
            await self._bind_workspace(name, sandbox.mount, create=True)
            await self._prove_the_mount(sandbox)
            row = (await self._listing()).get(name) or {}
        except BaseException:
            await self._discard(name)
            raise
        return self._sandbox(name, row, base, guest_mount)

    async def _prove_the_mount(self, sandbox: _SbxSandbox) -> None:
        """Have the guest read, through the mount and the wrapper, a file the host just wrote."""
        mount = sandbox.mount
        probe_name = f"{_PROBE_PREFIX}{secrets.token_hex(8)}"
        token = secrets.token_hex(16)
        await asyncio.to_thread((mount.host / probe_name).write_text, token, "ascii")
        try:
            probe = await sandbox.exec(
                ["cat", posixpath.join(mount.parent, probe_name)], working_directory="/", timeout=60
            )
        finally:
            await asyncio.to_thread((mount.host / probe_name).unlink, True)
        if probe.exit_code != 0 or probe.stdout != token:
            raise SbxError(
                f"the guest could not read the workspace at {mount.parent!r}; the image needs sh, "
                f"base64, setsid, mount and cat for this backend: {probe.stderr.strip()}"
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
        os.chmod(workspace, 0o777)


def _empty(workspace: Path) -> None:
    for child in workspace.iterdir():
        if child.is_dir() and not child.is_symlink() and not child.is_junction():
            shutil.rmtree(child)
        else:
            child.unlink()


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
