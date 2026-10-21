"""Observe native executable ancestry inside the smoke supervisor's Windows job."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import sys
from pathlib import Path
from typing import Any

from .evidence import Refused, require


class _ProcessIds(ctypes.Structure):
    _fields_ = [
        ("assigned", ctypes.wintypes.DWORD),
        ("count", ctypes.wintypes.DWORD),
        ("pids", ctypes.c_size_t * 4096),
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.wintypes.DWORD),
        ("usage", ctypes.wintypes.DWORD),
        ("pid", ctypes.wintypes.DWORD),
        ("heap", ctypes.c_size_t),
        ("module", ctypes.wintypes.DWORD),
        ("threads", ctypes.wintypes.DWORD),
        ("parent", ctypes.wintypes.DWORD),
        ("priority", ctypes.c_long),
        ("flags", ctypes.wintypes.DWORD),
        ("name", ctypes.wintypes.WCHAR * 260),
    ]


def sample(job: Any, executor: Path) -> dict[int, dict[str, Any]]:
    """Return owned process ancestry, binding the executor to its installed bytes."""
    if sys.platform != "win32":
        raise Refused("native_process_platform")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateToolhelp32Snapshot": ([ctypes.c_ulong, ctypes.c_ulong], ctypes.c_void_p),
        "Process32FirstW": ([ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)], ctypes.c_int),
        "Process32NextW": ([ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)], ctypes.c_int),
        "QueryFullProcessImageNameW": (
            [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)],
            ctypes.c_int,
        ),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    ids = _ProcessIds()
    job.check(
        job.api.QueryInformationJobObject(
            job.handle, 3, ctypes.byref(ids), ctypes.sizeof(ids), None
        )
    )
    require(ids.assigned <= 4096 and ids.count <= 4096, "native_process_inventory_limit")
    owned = set(ids.pids[: ids.count])
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    require(snapshot not in (None, ctypes.c_void_p(-1).value), "native_process_snapshot")
    rows: dict[int, dict[str, Any]] = {}
    try:
        entry = _ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.pid in owned:
                row: dict[str, Any] = {
                    "parent": entry.parent,
                    "name": entry.name,
                    "executor": False,
                }
                if entry.name.lower() == executor.name.lower():
                    process = job.api.OpenProcess(0x1000, False, entry.pid)
                    if process:
                        try:
                            path = ctypes.create_unicode_buffer(32768)
                            size = ctypes.c_ulong(len(path))
                            if api.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                                selected = Path(path.value).resolve()
                                if selected == executor:
                                    row["executor"] = True
                                    row["sha256"] = hashlib.sha256(
                                        selected.read_bytes()
                                    ).hexdigest()
                        finally:
                            job.api.CloseHandle(process)
                rows[entry.pid] = row
            more = api.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        job.api.CloseHandle(snapshot)
    return rows


def native_ancestor(rows: dict[int, dict[str, Any]], pid: int, expected_hash: str) -> bool:
    """Require a guest PID and a distinct verified executor in the same observation."""
    seen: set[int] = set()
    current = pid
    while current in rows and current not in seen:
        seen.add(current)
        row = rows[current]
        if current != pid and row.get("executor") and row.get("sha256") == expected_hash:
            return True
        current = row["parent"]
    return False
