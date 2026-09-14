"""Linux ownership and cgroup v2 containment for native workers."""

from __future__ import annotations

import ctypes
import errno
import math
import os
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import suppress

from ._wire import HyperlightWorkerError

_owner_guard = threading.Lock()
_owner_fd: int | None = None
_owner_pid: int | None = None
_LOCK_PATH = "/run/lock/maf-sandbox-hyperlight.lock"
DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup/maf-sandbox-hyperlight"


def _after_fork() -> None:
    """Drop inherited ownership while retaining the parent's PID for fork-use refusal."""
    global _owner_fd
    try:
        if _owner_fd is not None:
            os.close(_owner_fd)
            _owner_fd = None
    finally:
        _owner_guard.release()


if sys.platform == "linux":
    os.register_at_fork(
        before=_owner_guard.acquire,
        after_in_parent=_owner_guard.release,
        after_in_child=_after_fork,
    )


def _publish_owner_lock() -> None:
    """Publish the final read-only mode without replacing another creator's lock inode."""
    fd, temporary = tempfile.mkstemp(
        prefix=".maf-hyperlight-lock-", dir=os.path.dirname(_LOCK_PATH)
    )
    try:
        os.fchmod(fd, 0o444)
        rename = ctypes.CDLL(None, use_errno=True).renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        # RENAME_NOREPLACE keeps simultaneous creators on the same persistent inode.
        if rename(-100, os.fsencode(temporary), -100, os.fsencode(_LOCK_PATH), 1) != 0:
            error = ctypes.get_errno()
            if error != errno.EEXIST:
                raise OSError(error, "cannot publish the Hyperlight owner lock")
    finally:
        os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def claim_host() -> None:
    """Hold one owner in the shared lock-file namespace for the process lifetime."""
    import fcntl

    global _owner_fd, _owner_pid
    if _owner_pid is not None and _owner_pid != os.getpid():
        raise HyperlightWorkerError("a forked process cannot inherit the Hyperlight backend")
    with _owner_guard:
        if _owner_fd is not None:
            return
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(_LOCK_PATH, flags)
        except FileNotFoundError:
            _publish_owner_lock()
            fd = os.open(_LOCK_PATH, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HyperlightWorkerError("Hyperlight requires a regular, persistent owner lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise HyperlightWorkerError(
                    "another process owns Hyperlight; route requests and purges to that host"
                ) from error
            current = os.stat(_LOCK_PATH, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise HyperlightWorkerError("Hyperlight owner lock was replaced")
        except BaseException:
            os.close(fd)
            raise
        _owner_fd, _owner_pid = fd, os.getpid()


def check_kvm() -> None:
    """Verify KVM access before the SDK selects a hypervisor."""
    import fcntl

    try:
        with suppress(FileNotFoundError):
            os.stat("/dev/mshv")
            raise HyperlightWorkerError("Linux MSHV is not validated; use a KVM-only host")
        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        try:
            if fcntl.ioctl(fd, 0xAE00, 0) != 12:  # KVM_GET_API_VERSION
                raise HyperlightWorkerError("unsupported KVM API version")
            os.close(fcntl.ioctl(fd, 0xAE01, 0))  # KVM_CREATE_VM
        finally:
            os.close(fd)
    except OSError as error:
        raise HyperlightWorkerError(
            "KVM initialization failed; enable virtualization and grant /dev/kvm access"
        ) from error


def write_control(directory: int, name: str, value: str) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
    try:
        os.write(fd, value.encode("ascii"))
    finally:
        os.close(fd)


def read_control(directory: int, name: str) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
    try:
        return os.read(fd, 4096).decode("ascii")
    finally:
        os.close(fd)


def kill_group(directory: int, parent: int, name: str, deadline: float) -> None:
    """Kill the entire cgroup and remove it only after the kernel reports it empty."""
    try:
        write_control(directory, "cgroup.kill", "1")
        while "populated 1" in read_control(directory, "cgroup.events"):
            if time.monotonic() >= deadline:
                raise HyperlightWorkerError(
                    "Linux worker cgroup did not empty before cleanup expired"
                )
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        os.rmdir(name, dir_fd=parent)
    except OSError as error:
        # Both the host and its independent watcher may finish the same cleanup.
        if error.errno not in {errno.ENOENT, errno.ENODEV}:
            raise
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise


class Job:
    """A detached supervisor owns creation, containment and cleanup of the worker tree."""

    def __init__(self, memory_limit: int, root: str, timeout: float) -> None:
        self._pid = os.getpid()
        self._timeout = timeout
        self._root = self._owner = self._control = self._readiness = -1
        self._watcher: subprocess.Popen[bytes] | None = None
        self._name = "worker-" + uuid.uuid4().hex
        self._worker_pid: int | None = None
        self._closed = False
        self._ready = False
        try:
            self._root = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            filesystem = ctypes.create_string_buffer(256)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.fstatfs(self._root, ctypes.byref(filesystem)) != 0:
                raise OSError(ctypes.get_errno(), "cannot inspect the cgroup filesystem")
            if ctypes.c_long.from_buffer(filesystem).value != 0x63677270:
                raise HyperlightWorkerError("linux_cgroup_root must be on a cgroup v2 filesystem")
            page_size = os.sysconf("SC_PAGE_SIZE")
            self._memory_limit = memory_limit - memory_limit % page_size
            if self._memory_limit <= 0:
                raise HyperlightWorkerError("Linux worker memory must allow at least one page")
            self._owner = os.pidfd_open(self._pid)
        except BaseException as error:
            self._close_fds()
            if isinstance(error, OSError):
                raise HyperlightWorkerError(
                    "Linux workers require a writable delegated cgroup v2 root with memory, "
                    "swap and cgroup.kill controls; configure linux_cgroup_root"
                ) from error
            raise

    def _close_fds(self) -> None:
        for name in ("_root", "_owner", "_control", "_readiness"):
            fd = getattr(self, name)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)

    def spawn(
        self, command: list[str], *, environment: dict[str, str], cwd: str, cleanup_timeout: float
    ) -> subprocess.Popen[bytes]:
        """Pass ownership to supervision before creating any cgroup or native worker."""
        control, self._control = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            self._readiness, readiness = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
            try:
                self._watcher = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-u",
                        "-m",
                        "maf_sandbox_hyperlight._linux_watch",
                        str(self._owner),
                        str(self._root),
                        self._name,
                        str(self._memory_limit),
                        str(self._timeout),
                        str(control),
                        str(readiness),
                        *command,
                    ],
                    # Only the supervisor inherits ownership; worker descendants must not retain it.
                    pass_fds=(self._owner, self._root, control, readiness)
                    + ((_owner_fd,) if _owner_fd is not None else ()),
                    start_new_session=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=environment,
                )
            finally:
                os.close(readiness)
        finally:
            os.close(control)
        return self._watcher

    def ready(self, *, deadline: float) -> None:
        """Wait inside the supervised exchange so readiness consumes its startup deadline."""
        if self._ready:
            return
        poller = select.poll()
        poller.register(self._readiness, select.POLLIN)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not poller.poll(math.ceil(remaining * 1000)):
            raise TimeoutError("Linux worker lifetime watcher exceeded its startup deadline")
        try:
            worker = int(os.read(self._readiness, 64))
        except ValueError as error:
            raise HyperlightWorkerError("Linux worker lifetime watcher did not start") from error
        if worker <= 0:
            raise HyperlightWorkerError("Linux worker lifetime watcher did not start")
        self._worker_pid = worker
        self._ready = True

    def close(self, *, deadline: float | None = None) -> None:
        if os.getpid() != self._pid:
            raise HyperlightWorkerError("a forked process cannot dispose another owner's workers")
        if self._closed:
            return
        if deadline is None:
            deadline = time.monotonic() + self._timeout
        if self._watcher is not None:
            if self._watcher.poll() is None:
                with suppress(BrokenPipeError):
                    os.write(self._control, b"0")
            try:
                result = self._watcher.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise HyperlightWorkerError(
                    "Linux worker cleanup expired before confirmation"
                ) from error
            if result != 0:
                raise HyperlightWorkerError("Linux supervisor exited without cleanup confirmation")
        self._close_fds()
        self._closed = True
