"""Create and verify the local access boundary before opening retained state."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import re
import stat
import sys
from pathlib import Path

from .host_store import Refused


def _windows_access(path: Path) -> None:
    if sys.platform != "win32":
        raise Refused("Windows access evidence is unavailable")
    w = ctypes.wintypes
    pointer = ctypes.c_void_p
    out = ctypes.POINTER(pointer)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    kernel.CloseHandle.restype = w.BOOL
    kernel.LocalFree.argtypes = (pointer,)
    kernel.LocalFree.restype = pointer
    advapi.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = (
        w.HANDLE,
        w.DWORD,
        pointer,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    )
    advapi.GetTokenInformation.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = (pointer, ctypes.POINTER(w.LPWSTR))
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = (w.LPWSTR, w.DWORD, w.DWORD, out, out, out, out, out)
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        pointer,
        w.DWORD,
        w.DWORD,
        ctypes.POINTER(w.LPWSTR),
        ctypes.POINTER(w.DWORD),
    )
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL

    def sid_text(sid: int | None) -> str:
        value = w.LPWSTR()
        if not sid or not advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise Refused("store owner SID is unavailable")
        try:
            return value.value or ""
        finally:
            kernel.LocalFree(value)

    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not 0 < length.value <= 65536:
            raise Refused("current user token is unavailable")
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        user = sid_text(ctypes.cast(buffer, out).contents.value)
    finally:
        kernel.CloseHandle(token)

    owner, descriptor = pointer(), pointer()
    error = advapi.GetNamedSecurityInfoW(
        str(path), 1, 5, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)
    )
    if error:
        raise ctypes.WinError(error)
    value = w.LPWSTR()
    try:
        if sid_text(owner.value) != user:
            raise Refused("store root must belong to the current user")
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 4, ctypes.byref(value), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        _check_dacl(value.value or "", user)
    finally:
        if value:
            kernel.LocalFree(value)
        kernel.LocalFree(descriptor)


def _check_dacl(value: str, user: str) -> None:
    # Only simple allow ACEs for the owner and privileged OS administrators are supported.
    match = re.fullmatch(r"D:(?:P|AI|AR)*(\(.*\))", value)
    if match is None:
        raise Refused("store root needs a private supported DACL")
    aces = re.findall(r"\(([^()]*)\)", match[1])
    if "".join(f"({ace})" for ace in aces) != match[1]:
        raise Refused("store root has an unsupported DACL")
    for ace in aces:
        fields = ace.split(";")
        if (
            len(fields) != 6
            or fields[0] != "A"
            or re.fullmatch(r"(?:OI|CI|NP|IO|ID)*", fields[1]) is None
            or not fields[2]
            or fields[3:5] != ["", ""]
            or fields[5] not in {user, "OW", "SY", "BA", "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
        ):
            raise Refused("store root DACL permits other users or is unsupported")


def prepare(root: Path) -> None:
    """Refuse broad existing access; never repair permissions on retained data implicitly."""
    try:
        if root.is_symlink() or root.is_junction():
            raise Refused("store root must be private")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or root.is_junction():
            raise Refused("store root must be a private directory")
        if sys.platform == "win32":
            _windows_access(root)
        elif sys.platform == "linux":
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise Refused("store root must have private owner-only permissions")
        else:
            raise Refused("store access checks are unsupported on this platform")
    except OSError as error:
        raise Refused("store root access cannot be verified") from error


def check_file(path: Path) -> None:
    """Reject redirected storage files and independently readable Windows children."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or path.is_junction():
        raise Refused("store file must be a private regular file")
    if sys.platform == "win32":
        try:
            _windows_access(path)
        except OSError as error:
            raise Refused("store file access cannot be verified") from error
