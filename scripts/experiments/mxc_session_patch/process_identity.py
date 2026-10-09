"""Identify the local helper without treating a reused PID as the original process."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import platform
import select
import signal
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .host_store import Refused


@dataclass(frozen=True)
class Identity:
    """OS creation identity in the observer's machine and process namespace."""

    system: str
    machine: str
    pid: int
    created: str
    boot: str

    def encode(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return json.dumps({"identity": value, "sha256": hashlib.sha256(value.encode()).hexdigest()})

    @classmethod
    def decode(cls, value: str) -> Identity:
        try:
            envelope = json.loads(value)
            if (
                set(envelope) != {"identity", "sha256"}
                or not isinstance(envelope["identity"], str)
                or hashlib.sha256(envelope["identity"].encode()).hexdigest() != envelope["sha256"]
            ):
                raise ValueError("identity digest differs")
            fields = json.loads(envelope["identity"])
            identity = cls(**fields)
            if (
                type(identity.pid) is not int
                or not 0 < identity.pid <= 2**32 - 1
                or any(
                    type(v) is not str or not v
                    for v in (identity.system, identity.machine, identity.created, identity.boot)
                )
                or not identity.created.isdecimal()
                or len(value.encode()) > 1024
            ):
                raise ValueError("invalid fields")
            return identity
        except (KeyError, TypeError, ValueError) as error:
            raise Refused("invalid helper identity") from error


def _linux_boot() -> str:
    namespace = Path("/proc/self/ns/pid").stat()
    return f"{Path('/proc/sys/kernel/random/boot_id').read_text().strip()}:{namespace.st_dev}:{namespace.st_ino}"


def _linux_process(pid: int) -> tuple[str, bool] | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = value[value.rindex(")") + 2 :].split()
    return fields[19], fields[0] in ("Z", "X")


def _windows_process(pid: int, terminate_created: str | None = None) -> tuple[str, bool] | None:
    if sys.platform != "win32":
        raise Refused("Windows process evidence is unavailable on this platform")
    wintypes = ctypes.wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000 | 0x1000 | (1 if terminate_created else 0), False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        status = kernel.WaitForSingleObject(handle, 0)
        if status not in (0, 258):
            raise Refused("helper termination evidence is unavailable")
        created = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        if terminate_created == created and status == 258:
            kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel.TerminateProcess.restype = wintypes.BOOL
            if not kernel.TerminateProcess(handle, 1):
                error = ctypes.get_last_error()
                if error != 5:
                    raise ctypes.WinError(error)
            # Exit is asynchronous; access denied can mean termination is already underway.
            if kernel.WaitForSingleObject(handle, 15000) != 0:
                raise Refused("helper termination is unconfirmed")
            status = 0
        return created, status == 0
    finally:
        kernel.CloseHandle(handle)


def _observe(pid: int) -> tuple[str, tuple[str, bool] | None]:
    try:
        if os.name == "nt":
            return "creation-filetime", _windows_process(pid)
        if platform.system() == "Linux":
            return _linux_boot(), _linux_process(pid)
    except (OSError, ValueError, IndexError) as error:
        raise Refused("helper identity evidence is unavailable") from error
    raise Refused("helper identity is unsupported on this platform")


def capture(pid: int) -> Identity:
    """Capture a live helper while it is blocked on its private startup pipe."""
    boot, process = _observe(pid)
    if process is None or process[1]:
        raise Refused("helper exited before identity persistence")
    identity = Identity(platform.system(), platform.node(), pid, process[0], boot)
    return Identity.decode(identity.encode())


def stopped(identity: Identity) -> bool:
    """Require local OS evidence; permission failures never mean the helper is dead."""
    if (identity.system, identity.machine) != (platform.system(), platform.node()):
        raise Refused("helper belongs to a different machine")
    boot, process = _observe(identity.pid)
    if boot != identity.boot:
        # A different namespace is not proof of termination in the recorded namespace.
        raise Refused("helper boot or process namespace differs")
    return process is None or process[0] != identity.created or process[1]


def terminate(identity: Identity) -> None:
    """Stop only the recorded process through a stable OS handle; never signal a bare PID."""
    if stopped(identity):
        return
    try:
        if os.name == "nt":
            _windows_process(identity.pid, identity.created)
        elif sys.platform == "linux":
            # Pin the process before rechecking creation identity to exclude PID reuse.
            try:
                fd = os.pidfd_open(identity.pid)
            except ProcessLookupError:
                if stopped(identity):
                    return
                raise
            try:
                if stopped(identity):
                    return
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                poller = select.poll()
                poller.register(fd, select.POLLIN)
                if not poller.poll(15000):
                    raise Refused("helper termination is unconfirmed")
            finally:
                os.close(fd)
        else:
            raise Refused("stable process termination is unsupported")
    except (OSError, AttributeError) as error:
        raise Refused("helper termination evidence is unavailable") from error
    if not stopped(identity):
        raise Refused("helper termination is unconfirmed")
