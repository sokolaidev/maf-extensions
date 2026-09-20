"""Non-following Windows output handles retained through validation and reading."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from ctypes import wintypes
from pathlib import Path


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


def open_no_follow(path: Path, *, directory: bool = False) -> int:
    """Return an owned descriptor, refusing reparse points and allowing only read sharing."""
    if sys.platform == "win32":
        import msvcrt
    else:
        raise OSError("Windows output handles require Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    # OPEN_REPARSE_POINT addresses the entry itself; BACKUP_SEMANTICS permits directory handles.
    flags = 0x00200000 | (0x02000000 if directory else 0)
    # Root handles need only FILE_READ_ATTRIBUTES; file handles need GENERIC_READ.
    handle = api.CreateFileW(
        str(path), 0x80 if directory else 0x80000000, 0x1, None, 3, flags, None
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _AttributeTagInfo()
        if not api.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("output handle refers to a reparse point")
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        api.CloseHandle(handle)
        raise
