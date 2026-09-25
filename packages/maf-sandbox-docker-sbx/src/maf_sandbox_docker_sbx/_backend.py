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
from maf_sandbox.bounded_exec import SandboxExecOutputLimitExceeded, read_bounded_process_output
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
#: Every create mounts a fresh directory of its own, so `sbx ls` names whose create made a
#: sandbox and no VM ever mounts an earlier one's files.
_WORKSPACE_PREFIX = "ws-"
_META = "meta.json"
_META_VERSION = 1
_PROBE_PREFIX = ".maf-sbx-probe-"
#: Kept at the workspace root; its absence in the guest means the bind mount is gone.
_MARKER = ".maf-sbx-workspace"

#: Runs every command.  It runs nothing while the bind mount is missing, which an auto-stop
#: causes.  argv arrives base64-encoded behind an ``x`` so no argument is empty, which ``sbx``
#: refuses.  The nonce on stderr marks where the guest's own stderr begins, and proves the
#: wrapper ran.  ``setsid`` gives the command its own process group, recorded in the pid file, so
#: a deadline can kill the whole group.  The command runs only if no cancel file exists once
#: its group is recorded; see ``_KILL_SCRIPT`` for why that closes the race with a kill.
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
g='echo $$ > "$F" && [ ! -e "$F.cancel" ] && unset F && exec "$@"'
F=$f setsid -w sh -c "$g" sh "$@"
s=$?
rm -f "$f" "$f.cancel"
exit "$s"
"""

#: Ends an expired command, and only that command.  The cancel file goes down before the group
#: is read, and the wrapper checks for it after recording its group: so either the wrapper sees
#: it and never runs the command, or its group was recorded before this reads it.  Exit 4 is the
#: first case, a command that never started and now never will.
_KILL_SCRIPT = r""": > "$1.cancel" || exit 5
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
_NEVER_STARTED = 4

_AGENT_SOCKET = "/run/ssh-agent.sock"
#: Exits 1 when the SSH agent socket sbx forwards is present.
_NO_AGENT_SCRIPT = r"""[ ! -e "$1" ] && [ ! -L "$1" ]"""

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

#: Combined stdout and stderr one ``sbx`` command may return, a guest command's included.
_OUTPUT_LIMIT = 8 * 1024 * 1024

#: How long an interrupted create is watched for its sandbox.  With `sbx` 0.45.1 one appeared
#: within 0.4 s of its client being killed, or not at all.
_ABANDON_SETTLE_SECONDS = 10.0
_ABANDON_POLL_SECONDS = 0.5


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

    def _refuse_if_retired(self) -> None:
        if self._instance_id in self._backend.retired:
            raise SbxError(
                f"sandbox {self._name} was retired: an expired command's cleanup failed, so it "
                "may still run there. Acquire again for a replacement."
            )

    def _cwd(self, working_directory: str) -> str:
        self._refuse_if_retired()
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
            self._name,
            argv,
            cwd=cwd,
            timeout=timeout,
            mount=self._mount,
            instance=self._instance_id,
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
            # A POSIX host keeps names the guest path grammar refuses, such as `a\b`.
            relative = guest_path_relative_to(confine_resolve_guest_path(name, guest), cwd)
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
            instance=self._instance_id,
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
        #: Instances an expired command may still start or run in: every handle to one refuses,
        #: and acquire replaces it.  By instance, so a replacement under the same name is not.
        self._retired: set[str] = set()

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

    @property
    def retired(self) -> frozenset[str]:
        """Instance ids no handle may use again."""
        return frozenset(self._retired)

    # --- the CLI ------------------------------------------------------------------------

    async def _sbx(self, *args: str, timeout: float | None = None) -> _Result:
        """Run one ``sbx`` command; a timeout kills the client and raises ``TimeoutError``.

        Output past ``_OUTPUT_LIMIT`` kills it too and raises ``SandboxExecOutputLimitExceeded``.
        """
        process = await asyncio.create_subprocess_exec(
            self._config.sbx_path,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await read_bounded_process_output(
            process,
            max_output_bytes=_OUTPUT_LIMIT,
            timeout=timeout if timeout is not None else self._config.command_timeout_seconds,
        )
        return _Result(cast(int, process.returncode), stdout, stderr)

    async def run_in_guest(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        mount: _Mount,
        instance: str,
    ) -> ExecResult:
        """Run ``argv`` in ``cwd`` under the wrapper, killing its process group at ``timeout``.

        ``timeout`` covers a re-mount after an auto-stop as well as the command.  Output past
        ``_OUTPUT_LIMIT`` ends the command the same way, then raises.
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
            except (
                TimeoutError,
                SandboxExecOutputLimitExceeded,
                asyncio.CancelledError,
            ) as stopped:
                # Killing the client leaves the guest's command running in a reusable sandbox,
                # and a cancellation must not stop the kill either.
                await _to_the_end(self._end_the_command(name, instance, pid_file))
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

    async def _end_the_command(self, name: str, instance: str, pid_file: str) -> None:
        """Kill or cancel an expired command; retire the instance when that cannot be done.

        A retired instance refuses every further call, and the next acquire replaces it, since
        the command may still start or run there.  Only the expired command is touched: the
        sandbox may be running a sibling call of the same conversation.
        """
        try:
            killed = await self._kill_group(name, pid_file)
        except Exception as error:  # noqa: BLE001 - the caller's own exception must stand
            logger.warning("docker-sbx: the kill in %s failed: %s", name, error)
            killed = False
        if not killed:
            self._retired.add(instance)
            await asyncio.to_thread(self._record_retirement, name, instance)

    def _record_retirement(self, name: str, instance: str) -> None:
        """Mark the record, so a later process replaces the instance too; never raises."""
        meta = self._read_meta(name)
        if meta is None or meta.get("instance_id") != instance:
            return
        try:
            _write_record(self._directory(name) / _META, {**meta, "retired": True})
        except OSError as error:
            logger.warning("docker-sbx: could not record %s as retired: %s", name, error)

    def _is_retired(self, name: str, instance: object) -> bool:
        if instance in self._retired:
            return True
        meta = self._read_meta(name) or {}
        if meta.get("retired") is True and meta.get("instance_id") == instance:
            self._retired.add(str(instance))
            return True
        return False

    async def _kill_group(self, name: str, pid_file: str) -> bool:
        """Whether the expired command is known to be killed, or never to start."""
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
            return False
        except (OSError, SandboxExecOutputLimitExceeded) as error:
            # Raised past here it would replace the caller's own timeout or cancellation.
            logger.warning("docker-sbx: the kill in %s did not complete: %s", name, error)
            return False
        if result.returncode not in (0, _NEVER_STARTED):
            logger.warning(
                "docker-sbx: could not kill an expired command in %s (exit %s): %s",
                name,
                result.returncode,
                result.stderr_text.strip()[-_STDERR_TAIL:],
            )
            return False
        return True

    async def _listing(self, timeout: float | None = None) -> dict[str, dict[str, object]]:
        result = await self._sbx("ls", "--json", timeout=timeout)
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
        registered = _listed_servers(servers.stdout)
        if registered is None:
            raise SbxError(
                "`sbx mcp ls --json` gave no `servers` list, so whether an MCP server is "
                "registered cannot be told; refusing rather than assuming none is"
            )
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

    def _recorded_workspace(self, name: str, meta: dict[str, object]) -> Path:
        workspace = meta.get("workspace")
        if (
            not isinstance(workspace, str)
            or not workspace.startswith(_WORKSPACE_PREFIX)
            or Path(workspace).name != workspace
        ):
            raise SbxError(f"sandbox {name}'s record names no workspace. Dispose it.")
        return self._directory(name) / workspace

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
            if row is not None and self._is_retired(name, row.get("id")):
                failure = await self._remove(name, str(row.get("id")))
                if failure is not None:
                    raise SbxError(f"could not replace retired sandbox {name}: {failure}")
                row = None
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
                    await self._discard(name, sandbox.mount.host)
                raise
            return sandbox

    def _adopt(
        self, key: SandboxKey, name: str, row: dict[str, object], spec: SandboxSpec, base: str
    ) -> _SbxSandbox:
        meta = self._read_meta(name)
        if meta is None:
            raise SbxError(
                f"sandbox {name} has no record in {self._directory(name)}; it was created by "
                "another backend or its create was interrupted. Dispose it."
            )
        workspace = self._recorded_workspace(name, meta)
        if row.get("workspaces") != [str(workspace)]:
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
        return self._sandbox(name, row, base, guest_mount, workspace)

    def _sandbox(
        self, name: str, row: dict[str, object], base: str, guest_mount: str, workspace: Path
    ) -> _SbxSandbox:
        instance_id = row.get("id")
        if not isinstance(instance_id, str) or not instance_id:
            raise SbxError(
                f"`sbx ls` gives sandbox {name} no id, so it could not be disposed exactly; the "
                "daemon may have lost its engine. Run `sbx daemon restart`."
            )
        mount = _Mount(guest_mount, posixpath.dirname(base), workspace)
        plane = WorkspacePlane(workspace, posixpath.dirname(base))
        return _SbxSandbox(self, name, instance_id, base, plane, mount)

    async def _create(
        self, name: str, key: SandboxKey, spec: SandboxSpec, base: str
    ) -> tuple[_SbxSandbox, bool]:
        """The sandbox, and whether this call created it rather than adopting a winner's."""
        directory = self._directory(name)
        workspace = directory / f"{_WORKSPACE_PREFIX}{secrets.token_hex(6)}"
        root = self._config.resolved_workspace_root
        await asyncio.to_thread(_make_private, root, directory, workspace)
        meta: dict[str, object] = {
            "version": _META_VERSION,
            "key": _key_record(key),
            "kind": spec.kind,
            "work_dir": base,
            "image": spec.image,
            "image_id": spec.image_id,
            "workspace": workspace.name,
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
            # The daemon may finish a create whose client was stopped.
            await _to_the_end(self._abandon_create(name, workspace))
            raise
        if created.returncode != 0:
            await asyncio.to_thread(_remove_if_empty, workspace, directory)
            if _ALREADY_EXISTS in created.stderr_text:
                return await self._adopt_the_winner(key, name, spec, base), False
            raise _failure(f"sbx create {name}", created)
        try:
            mounted = await self._run_setup(
                "reading the workspace mount", "exec", name, "sh", "-c", "pwd -P"
            )
            guest_mount = mounted.stdout.decode("utf-8", errors="replace").strip()
            if not guest_mount.startswith("/") or "\n" in guest_mount:
                raise SbxError(f"the guest reported {guest_mount!r} as its workspace mount")
            meta["guest_mount"] = guest_mount
            # The real id before any command runs, so a failed cleanup retires this instance.
            row = (await self._listing()).get(name) or {}
            sandbox = self._sandbox(name, row, base, guest_mount, workspace)
            await self._bind_workspace(name, sandbox.mount, create=True)
            await self._prove_the_mount(sandbox)
            await self.check_sandbox(name)
            # Last, so a record another process can read means the sandbox is ready to serve.
            meta["instance_id"] = sandbox.instance_id
            await asyncio.to_thread(_write_record, directory / _META, meta)
        except BaseException:
            await self._discard(name, workspace)
            raise
        return sandbox, True

    async def _abandon_create(self, name: str, workspace: Path) -> None:
        """Remove a stopped create's sandbox, which the daemon may list only after a moment.

        Only one mounting this create's workspace is removed.  The workspace is kept while none
        has appeared, so a sandbox the daemon finishes even later still has it.  Never raises.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ABANDON_SETTLE_SECONDS
        while (left := deadline - loop.time()) > 0:
            try:
                row = (await self._listing(timeout=left)).get(name)
            except (SbxError, OSError, ValueError):
                return
            if row is not None:
                if row.get("workspaces") == [str(workspace)]:
                    await self._discard(name, workspace)
                return
            await asyncio.sleep(min(_ABANDON_POLL_SECONDS, max(0.0, deadline - loop.time())))

    async def check_sandbox(self, name: str) -> None:
        """Refuse a new sandbox the running daemon gave the host's SSH agent.

        The setting ``check_host`` reads takes effect only after ``sbx daemon restart``.
        """
        checked = await self._sbx(
            "exec", name, "sh", "-c", _NO_AGENT_SCRIPT, "maf-sbx", _AGENT_SOCKET
        )
        if checked.returncode == 1:
            raise SbxHostNotConfined(
                f"sandbox {name} has the host's SSH agent at {_AGENT_SOCKET}: the running daemon "
                "still forwards it. Run `sbx daemon restart` after "
                "`sbx settings set ssh.agentForwardingEnabled false`."
            )
        if checked.returncode != 0:
            raise _failure(f"checking sandbox {name} for a forwarded SSH agent", checked)

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

    async def _discard(self, name: str, workspace: Path) -> None:
        """Remove what a failed acquire created; never raises, so the acquire's error stands."""
        try:
            failure = await _to_the_end(self._remove(name, None, workspace.name))
        except Exception as error:  # noqa: BLE001 - reported, not raised
            failure = DisposalFailure("unknown", f"{type(error).__name__}: {error}")
        if failure is not None:
            logger.warning(
                "docker-sbx: could not remove %s after a failed acquire: %s", name, failure
            )

    # --- disposal -----------------------------------------------------------------------

    async def _remove(
        self, name: str, instance_id: str | None, generation: str | None = None
    ) -> DisposalFailure | None:
        """Delete one sandbox, then what it left on the host; absence is success.

        `sbx rm` frees the name for every process, and a replacement makes its own generation in
        the same directory.  So only what was there before the removal is deleted: the instance's
        generation, the one ``generation`` names, or else everything the directory held.
        """
        directory = self._directory(name)
        if instance_id is not None:
            try:
                row = (await self._listing()).get(name)
            except (SbxError, TimeoutError, ValueError) as error:
                return DisposalFailure("unlisted", str(error))
            if row is None and await asyncio.to_thread(directory.exists):
                return await self._remove_unlisted(name, instance_id)
            if row is None or row.get("id") != instance_id:
                return None
            generation = _generation_of(directory, row)
        if generation is not None:
            held = {generation}
        elif instance_id is None:
            held = await asyncio.to_thread(_entries, directory)
        else:
            held = set[str]()
        try:
            removed = await self._sbx("rm", "--force", name)
        except TimeoutError:
            return DisposalFailure("timeout", f"sbx rm {name} timed out")
        if removed.returncode != 0 and _NOT_FOUND not in removed.stderr_text:
            error = _failure(f"sbx rm {name}", removed)
            code = "unreachable" if isinstance(error, SbxDaemonFault) else "refused"
            return DisposalFailure(code, str(error))
        try:
            await asyncio.to_thread(_clear, directory, held)
        except OSError as error:
            return DisposalFailure("unknown", f"removing {name}'s workspace: {error}")
        return None

    async def _remove_unlisted(self, name: str, instance_id: str) -> DisposalFailure | None:
        """Clear the workspace of an instance the listing omits, once the daemon says it is gone.

        A daemon that has lost its engine lists nothing, so only the sandbox itself can answer.
        """
        try:
            await self._confirm_absent(name)
        except TimeoutError:
            return DisposalFailure("timeout", f"asking whether {name} is there timed out")
        except SbxDaemonFault as error:
            return DisposalFailure("unreachable", str(error))
        except SbxError as error:
            return DisposalFailure("unknown", str(error))
        meta = self._read_meta(name) or {}
        generation = meta.get("workspace")
        if meta.get("instance_id") != instance_id or not isinstance(generation, str):
            return None
        try:
            await asyncio.to_thread(_clear, self._directory(name), {generation})
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


async def _to_the_end[T](work: Awaitable[T]) -> T:
    """Finish ``work`` through every cancellation, then re-raise one.

    Cleanup must land before its caller releases the lock and handle it runs for.
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


def _make_private(root: Path, directory: Path, workspace: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for path in (directory, workspace):
        path.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(root, 0o700)


def _remove_if_empty(*directories: Path) -> None:
    for directory in directories:
        with contextlib.suppress(OSError):
            directory.rmdir()


def _write_record(path: Path, meta: dict[str, object]) -> None:
    part = path.with_name(f".{path.name}.{secrets.token_hex(8)}.part")
    part.write_text(json.dumps(meta), "utf-8")
    os.replace(part, path)


def _listed_servers(stdout: bytes) -> list[object] | None:
    """The servers `sbx mcp ls --json` names, or ``None`` for any other shape."""
    try:
        payload: object = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    servers = cast("dict[str, object]", payload).get("servers")
    return cast("list[object]", servers) if isinstance(servers, list) else None


def _generation_of(directory: Path, row: dict[str, object]) -> str | None:
    """The workspace generation a listed sandbox mounts, if it is one of ``directory``'s."""
    workspaces = row.get("workspaces")
    if not isinstance(workspaces, list):
        return None
    listed = cast("list[object]", workspaces)
    if len(listed) != 1 or not isinstance(listed[0], str):
        return None
    path = Path(listed[0])
    if path.parent != directory or not path.name.startswith(_WORKSPACE_PREFIX):
        return None
    return path.name


def _entries(directory: Path) -> set[str]:
    try:
        return {entry.name for entry in directory.iterdir()}
    except FileNotFoundError:
        return set()


def _clear(directory: Path, held: set[str]) -> None:
    """Delete ``held`` from a name's directory, then the directory if nothing else is in it."""
    for name in held - {_META}:
        if name in ("", ".", "..") or Path(name).name != name:
            continue
        path = directory / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    _drop_record(directory, held)
    _remove_if_empty(directory)


def _drop_record(directory: Path, held: set[str]) -> None:
    """Remove the name's record unless it describes a generation outside ``held``."""
    record = directory / _META
    detached = directory / f".{_META}.{secrets.token_hex(8)}.drop"
    try:
        os.replace(record, detached)
    except FileNotFoundError:
        return
    try:
        workspace = json.loads(detached.read_text("utf-8")).get("workspace")
    except (ValueError, AttributeError):
        workspace = None
    if isinstance(workspace, str) and workspace not in held:
        # A replacement's: put it back, unless it has already written a newer one.
        with contextlib.suppress(FileExistsError):
            os.link(detached, record)
    detached.unlink()


def _directories_named(root: Path, prefix: str) -> set[str]:
    try:
        return {entry.name for entry in root.iterdir() if entry.name.startswith(prefix)}
    except FileNotFoundError:
        return set()


# Checks the full protocol signatures, which `runtime_checkable` does not.
if TYPE_CHECKING:
    _: tuple[SandboxBackend, type[Sandbox]] = (SbxSandboxBackend(), _SbxSandbox)
