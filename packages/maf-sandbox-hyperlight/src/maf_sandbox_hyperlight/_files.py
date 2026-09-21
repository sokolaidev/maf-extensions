"""Trusted, flat output storage for the pinned guest's /output preopen."""

from __future__ import annotations

import os
import posixpath
import shutil
import stat
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from maf_sandbox import EntryKind, SandboxEntry, SandboxTransferCapExceeded
from maf_sandbox.paths import confine_resolve_guest_path, resolve_guest_working_directory

from ._windows_files import directory_names, open_no_follow

GUEST_ROOT = "/output"
MAX_LIST_ENTRIES = 64
MAX_LIST_NAME_BYTES = 64 * 1024


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _entry(path: str, info: os.stat_result) -> SandboxEntry:
    kind = (
        EntryKind.SYMLINK
        if _is_link(info)
        else EntryKind.DIRECTORY
        if stat.S_ISDIR(info.st_mode)
        else EntryKind.FILE
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        else EntryKind.OTHER
    )
    return SandboxEntry(path, kind, info.st_size if kind is EntryKind.FILE else None)


def _validate_name(name: str) -> None:
    device = name.split(".", 1)[0].upper()
    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(c in name for c in '/\\\0:<>"|?*')
        or any(ord(c) < 32 for c in name)
        or device in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        or device in {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³"}
    ):
        raise ValueError("unsupported output filename")


class OutputDirectory:
    """A parent-owned directory retained until its worker and readers have stopped."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="maf-hyperlight-")).resolve()
        info = self.path.lstat()
        self._identity = (info.st_dev, info.st_ino)

    @contextmanager
    def _opened_root(self, *, listing: bool = False) -> Generator[int]:
        if sys.platform == "win32":
            root = open_no_follow(self.path, directory=True, list_directory=listing)
        else:
            root = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(root)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._identity:
                raise ValueError("the output directory was replaced")
            yield root
        finally:
            os.close(root)

    @contextmanager
    def _reader(self, target: Path) -> Generator[BinaryIO]:
        with self._opened_root() as root:
            if sys.platform == "win32":
                descriptor = open_no_follow(target)
            else:
                descriptor = os.open(
                    target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root
                )
            try:
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    yield stream
            finally:
                os.close(descriptor)

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
            _validate_name(part)
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
        return _entry(relative, info)

    def _names(self, root: int) -> Generator[tuple[str, int]]:
        if sys.platform == "win32":
            yield from directory_names(root)
        else:
            with os.scandir(root) as entries:
                for entry in entries:
                    yield entry.name, entry.inode()

    def list_dir(self, path: str, working_directory: str) -> tuple[SandboxEntry, ...]:
        """List the prepared base, refusing incomplete or redirected enumeration."""
        target, _ = self._path(path, working_directory)
        with self._opened_root(listing=True) as root:
            if target != self.path:
                info = self._stat_child(root, target.name)
                if _is_link(info):
                    raise ValueError("output listing target is a link")
                if not stat.S_ISDIR(info.st_mode):
                    raise NotADirectoryError(path)
                raise ValueError("the pinned Hyperlight guest supports only flat output files")
            entries: dict[str, SandboxEntry] = {}
            name_bytes = 0
            names = self._names(root)
            try:
                for name, identity in names:
                    name_bytes += len(name.encode("utf-8"))
                    if len(entries) >= MAX_LIST_ENTRIES or name_bytes > MAX_LIST_NAME_BYTES:
                        raise SandboxTransferCapExceeded(
                            "output listing exceeds its metadata budget"
                        )
                    _validate_name(name)
                    if name in entries:
                        raise OSError("output listing contains a duplicate name")
                    info = self._stat_child(root, name)
                    if (info.st_dev, info.st_ino) != (self._identity[0], identity):
                        raise OSError("output entry was replaced during listing")
                    entries[name] = _entry(name, info)
            finally:
                names.close()
            return tuple(entries[name] for name in sorted(entries))

    def _stat_child(self, root: int, name: str) -> os.stat_result:
        if sys.platform == "win32":
            # The root denies rename/delete; compare each fresh identity with its handle listing.
            return (self.path / name).lstat()
        return os.stat(name, dir_fd=root, follow_symlinks=False)

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
        with self._reader(target) as stream:
            info = os.fstat(stream.fileno())
            if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
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
