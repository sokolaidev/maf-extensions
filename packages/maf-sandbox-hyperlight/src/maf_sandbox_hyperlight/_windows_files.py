"""Non-following Windows output handles retained through validation and reading."""

from __future__ import annotations

import ctypes
import os
import stat
import struct
import sys
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


def open_no_follow(path: Path, *, directory: bool = False, list_directory: bool = False) -> int:
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
    # Enumeration additionally needs FILE_LIST_DIRECTORY on the pinned root.
    handle = api.CreateFileW(
        str(path),
        (0x80 | int(list_directory)) if directory else 0x80000000,
        0x1,
        None,
        3,
        flags,
        None,
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


_DIRECTORY_HEADER = struct.Struct("<IIqqqqqqIIII16s")


def _directory_records(buffer: bytes) -> Iterator[tuple[str, int]]:
    offset = 0
    while True:
        if offset + _DIRECTORY_HEADER.size > len(buffer):
            raise OSError("incomplete directory metadata")
        fields = _DIRECTORY_HEADER.unpack_from(buffer, offset)
        following, name_bytes = int(fields[0]), int(fields[9])
        start = offset + _DIRECTORY_HEADER.size
        end = start + name_bytes
        if not name_bytes or name_bytes % 2 or end > len(buffer):
            raise OSError("invalid directory filename metadata")
        if following and (
            following % 8 or following < end - offset or offset + following >= len(buffer)
        ):
            raise OSError("invalid directory metadata offset")
        name = buffer[start:end].decode("utf-16-le")
        if name not in {".", ".."}:
            yield name, int.from_bytes(fields[12], "little")
        if not following:
            return
        offset += following


def directory_names(root: int) -> Iterator[tuple[str, int]]:
    """Enumerate names and 128-bit identities through an open directory handle."""
    if sys.platform != "win32":
        raise OSError("Windows directory enumeration requires Windows")
    import msvcrt

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(64 * 1024)
    information_class = 20  # FileIdExtdDirectoryRestartInfo, then FileIdExtdDirectoryInfo.
    while True:
        if not api.GetFileInformationByHandleEx(
            msvcrt.get_osfhandle(root), information_class, buffer, ctypes.sizeof(buffer)
        ):
            error = ctypes.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES is the only successful terminator.
                return
            raise ctypes.WinError(error)
        yield from _directory_records(buffer.raw)
        information_class = 19
