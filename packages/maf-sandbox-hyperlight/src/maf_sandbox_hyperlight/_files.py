"""Trusted, flat output storage for the pinned guest's /output preopen."""

from __future__ import annotations

import os
import posixpath
import shutil
import stat
import tempfile
from pathlib import Path

from maf_sandbox import EntryKind, SandboxEntry, SandboxTransferCapExceeded
from maf_sandbox.paths import confine_resolve_guest_path, resolve_guest_working_directory

GUEST_ROOT = "/output"


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


class OutputDirectory:
    """A parent-owned directory retained until its worker and readers have stopped."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="maf-hyperlight-")).resolve()
        info = self.path.lstat()
        self._identity = (info.st_dev, info.st_ino)

    def _root(self) -> None:
        info = self.path.lstat()
        if _is_link(info) or (info.st_dev, info.st_ino) != self._identity:
            raise ValueError("the output directory was replaced")

    def _path(self, path: str, working_directory: str) -> tuple[Path, str]:
        self._root()
        if not path or path.startswith("/") or any(c in path for c in ("\\", "\0", ":")):
            raise ValueError("output names must be relative POSIX paths")
        for value in (path, working_directory):
            if ".." in value.split("/") or any(c in value for c in ("\\", "\0", ":")):
                raise ValueError("output paths cannot traverse outside their directory")
        cwd = resolve_guest_working_directory(working_directory, GUEST_ROOT)
        if cwd != GUEST_ROOT and not cwd.startswith(GUEST_ROOT + "/"):
            raise ValueError("working directory is outside /output")
        guest = confine_resolve_guest_path(path, cwd)
        relative = posixpath.relpath(guest, GUEST_ROOT)
        parts = [] if relative == "." else relative.split("/")
        target = self.path
        for index, part in enumerate(parts):
            device = part.split(".", 1)[0].upper()
            if (
                part.endswith((".", " "))
                or any(c in part for c in '<>"|?*')
                or any(ord(c) < 32 for c in part)
                or device in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or device in {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³"}
            ):
                raise ValueError("unsupported output filename")
            target /= part
            if index < len(parts) - 1 or (guest == cwd and cwd != GUEST_ROOT):
                try:
                    info = target.lstat()
                except FileNotFoundError:
                    break
                if _is_link(info):
                    raise ValueError("output path passes through a link")
                if not stat.S_ISDIR(info.st_mode):
                    raise NotADirectoryError("output path passes through a non-directory")
        if len(parts) > 1:
            raise ValueError("the pinned Hyperlight guest supports only flat output files")
        return target, posixpath.normpath(path)

    def stat_file(self, path: str, working_directory: str) -> SandboxEntry | None:
        """Inspect without following the final entry; classify unexpected links honestly."""
        target, relative = self._path(path, working_directory)
        try:
            info = target.lstat()
        except FileNotFoundError:
            return None
        kind = (
            EntryKind.SYMLINK
            if _is_link(info)
            else EntryKind.DIRECTORY
            if stat.S_ISDIR(info.st_mode)
            else EntryKind.FILE
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            else EntryKind.OTHER
        )
        return SandboxEntry(relative, kind, info.st_size if kind is EntryKind.FILE else None)

    def read_file(self, path: str, working_directory: str, max_bytes: int) -> bytes:
        """Read a regular file, refusing overflow rather than returning a prefix."""
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        entry = self.stat_file(path, working_directory)
        if entry is None:
            raise FileNotFoundError(path)
        if entry.kind is not EntryKind.FILE:
            raise OSError("only regular output files can be read")
        if entry.size_bytes is None or entry.size_bytes > max_bytes:
            raise SandboxTransferCapExceeded("output exceeds max_bytes")
        target, _ = self._path(path, working_directory)
        descriptor = os.open(
            target,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("output is not a regular unlinked file")
            if info.st_size > max_bytes:
                raise SandboxTransferCapExceeded("output exceeds max_bytes")
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise SandboxTransferCapExceeded("output exceeds max_bytes")
        return content

    def validate(self) -> None:
        """Refuse unexpected entries before entering native preparation or guest execution."""
        self._root()
        for child in self.path.iterdir():
            info = child.lstat()
            if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("the output directory must contain only regular, unlinked files")

    def clear(self) -> None:
        """Clear the flat tree after restore has invalidated native output handles."""
        self.validate()
        for child in self.path.iterdir():
            child.unlink()

    def close(self) -> None:
        """Delete owned storage after worker termination; leave failures retryable."""
        try:
            self.path.lstat()
        except FileNotFoundError:
            return
        self._root()
        shutil.rmtree(self.path)
