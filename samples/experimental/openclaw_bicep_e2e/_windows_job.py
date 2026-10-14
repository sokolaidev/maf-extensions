"""Windows job ownership for service wrappers and their descendants."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes


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


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("Times", ctypes.c_int64 * 4),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ProcessIds(ctypes.Structure):
    _fields_ = [
        ("assigned", wintypes.DWORD),
        ("count", wintypes.DWORD),
        ("pids", ctypes.c_size_t * 4096),
    ]


class Job:
    """Hold a non-inherited job handle until its active process count reaches zero."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows jobs require Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "QueryInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        self.check(self.handle)
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self.check(
                self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(result: object) -> None:
        if not result:
            if os.name != "nt":
                raise OSError("Windows jobs require Windows")
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid: int) -> None:
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        self.check(process)
        try:
            self.check(self.api.AssignProcessToJobObject(self.handle, process))
        finally:
            self.check(self.api.CloseHandle(process))

    def active(self) -> int:
        info = _Accounting()
        self.check(
            self.api.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
            )
        )
        return info.ActiveProcesses

    def terminate(self) -> None:
        ids = _ProcessIds()
        handles = []
        try:
            self.check(
                self.api.QueryInformationJobObject(
                    self.handle, 3, ctypes.byref(ids), ctypes.sizeof(ids), None
                )
            )
            for pid in ids.pids[: ids.count]:
                process = self.api.OpenProcess(0x100000, False, pid)
                if process:
                    handles.append(process)
                elif os.name == "nt" and ctypes.get_last_error() != 87:
                    self.check(process)
        finally:
            # Termination must still run if enumeration or opening a handle fails.
            try:
                self.check(self.api.TerminateJobObject(self.handle, 1))
                end = time.monotonic() + 30
                for process in handles:
                    remaining = max(0, int((end - time.monotonic()) * 1000))
                    if self.api.WaitForSingleObject(process, remaining) != 0:
                        raise TimeoutError("Job process termination did not settle")
            finally:
                for process in handles:
                    self.check(self.api.CloseHandle(process))

    def close(self) -> None:
        if self.handle:
            self.check(self.api.CloseHandle(self.handle))
            self.handle = None
