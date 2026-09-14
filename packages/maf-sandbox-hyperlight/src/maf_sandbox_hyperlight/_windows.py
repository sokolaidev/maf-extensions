"""A parent-owned Windows job bounds worker lifetime and committed memory."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from ctypes import wintypes

from ._wire import HyperlightWorkerError

_owner_guard = threading.Lock()
_owner_handle: int | None = None


def claim_host() -> None:
    """Keep one backend host process per machine; a foreign scope purge must fail visibly."""
    global _owner_handle
    with _owner_guard:
        if _owner_handle is not None:
            return
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        api.CreateEventW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        handle = api.CreateEventW(None, True, False, "Global\\maf-sandbox-hyperlight-host-v1")
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            api.CloseHandle(handle)
            raise HyperlightWorkerError(
                "another process owns Hyperlight; route requests and purges to that host"
            )
        _owner_handle = handle


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class Job:
    """Kill all assigned processes when the owning host closes this job or exits."""

    def __init__(self, memory_limit: int) -> None:
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        self._api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._api.CreateJobObjectW.restype = wintypes.HANDLE
        self._api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._api.SetInformationJobObject.restype = wintypes.BOOL
        self._api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._api.OpenProcess.restype = wintypes.HANDLE
        self._api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._api.AssignProcessToJobObject.restype = wintypes.BOOL
        self._api.CloseHandle.argtypes = [wintypes.HANDLE]
        self._api.CloseHandle.restype = wintypes.BOOL
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        # KILL_ON_JOB_CLOSE and JOB_MEMORY apply to the whole worker tree.
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x200
        limits.JobMemoryLimit = memory_limit
        if not self._api.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def spawn(
        self, command: list[str], *, environment: dict[str, str], cwd: str, cleanup_timeout: float
    ) -> subprocess.Popen[bytes]:
        """Assign the waiting worker before its first initialization request."""
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            self.assign(process.pid)
        except BaseException:
            deadline = time.monotonic() + cleanup_timeout
            try:
                process.kill()
                process.wait(timeout=max(0, deadline - time.monotonic()))
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            raise
        return process

    def assign(self, pid: int) -> None:
        process = self._api.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._api.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._api.CloseHandle(process)

    def ready(self, *, deadline: float) -> None:
        """Job assignment establishes Windows containment synchronously."""

    def close(self, *, deadline: float | None = None) -> None:
        if self._handle:
            if not self._api.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
