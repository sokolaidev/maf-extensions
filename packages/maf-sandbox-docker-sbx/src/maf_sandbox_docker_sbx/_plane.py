"""The workspace file plane: stats, reads, listings and writes on the host side of the mount.

Each sandbox mounts one private host directory, and the guest reaches it through a link the
backend places at the storage base's parent.  The plane maps a guest path under that parent
onto the host directory, so no stat is answered by the guest.  Removals are not here: a guest
keeps seeing a name for seconds after the host deletes it, so the backend removes in the guest.

On a POSIX host every component is opened with ``O_NOFOLLOW`` relative to its parent's
descriptor, so a link the guest makes between the check and the operation fails the operation
rather than redirecting it.  Windows has no descriptor-relative calls; there the plane checks
each component for a reparse point and acts by path, which rests on the guest being unable to
create a link in its workspace (measured on sbx v0.45.1).

Names the host filesystem would change, hide or merge are refused, because the host would then
act on a different file than the guest named.
"""

from __future__ import annotations

import errno
import os
import posixpath
import secrets
import stat as stat_module
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from maf_sandbox import EntryKind, SandboxEntry, SandboxTransferCapExceeded
from maf_sandbox.paths import guest_path_relative_to

__all__ = ["WorkspacePlane", "refuse_names_the_host_changes"]

_WINDOWS = sys.platform == "win32"

#: Characters a Windows filesystem refuses or silently remaps, and every control character.
_WINDOWS_RESERVED = frozenset('<>:"|?*') | frozenset(chr(code) for code in range(32))
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{digit}" for port in ("COM", "LPT") for digit in "0123456789¹²³"}
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_PART_PREFIX = ".maf-sbx-"
# Read through getattr: they are absent on Windows, where the POSIX plane never runs.
_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)
_O_BINARY: int = getattr(os, "O_BINARY", 0)
_READ_CHUNK = 1 << 20


def refuse_names_the_host_changes(parts: Sequence[str], *, windows: bool = _WINDOWS) -> None:
    """Refuse a component the host filesystem would store under a different name, or not at all.

    Case folding and short-name aliases are caught separately, against the directory listing.
    """
    for name in parts:
        if "\0" in name:
            raise ValueError(f"{name!r} contains a NUL byte")
        if not windows:
            continue
        if any(character in _WINDOWS_RESERVED for character in name):
            raise ValueError(
                f"{name!r} contains a character a Windows host filesystem refuses or remaps, "
                "so the host would not store the name the guest uses"
            )
        if name.endswith((".", " ")):
            raise ValueError(
                f"{name!r} ends in a dot or a space, which a Windows host filesystem may strip"
            )
        if name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICES:
            raise ValueError(f"{name!r} names a Windows device rather than a file")


def _entry_kind(mode: int, attributes: int = 0) -> EntryKind:
    if stat_module.S_ISLNK(mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return EntryKind.SYMLINK
    if stat_module.S_ISDIR(mode):
        return EntryKind.DIRECTORY
    if stat_module.S_ISREG(mode):
        return EntryKind.FILE
    return EntryKind.OTHER


def _kind(result: os.stat_result) -> EntryKind:
    return _entry_kind(result.st_mode, getattr(result, "st_file_attributes", 0))


def _linked(name: str) -> ValueError:
    return ValueError(
        f"{name!r} is a link rather than a real directory, so a path through it "
        "does not stay inside the workspace"
    )


class _Directory(Protocol):
    """One open directory of the plane, and the operations the plane performs inside it."""

    def lstat(self, name: str) -> os.stat_result | None: ...
    def names(self) -> list[str]: ...
    def child(self, name: str, *, create: bool) -> _Directory: ...
    def close(self) -> None: ...
    def __enter__(self) -> _Directory: ...
    def __exit__(self, *exc: object) -> None: ...
    def read(self, name: str, max_bytes: int) -> bytes: ...
    def write(self, name: str, content: bytes) -> None: ...


def _refuse_an_alias(directory: _Directory, name: str, found: os.stat_result | None) -> None:
    # A name that stats but is not listed verbatim reached a different entry: case folding,
    # Unicode normalisation or an 8.3 short name.
    if found is not None and name not in directory.names():
        raise ValueError(
            f"{name!r} is not stored verbatim on the host, which merges it with another name"
        )


class _PosixDirectory:
    """A directory held open by descriptor; each child is opened relative to it."""

    _FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW

    def __init__(self, fd: int) -> None:
        self._fd = fd

    @classmethod
    def open_root(cls, root: Path) -> _PosixDirectory:
        return cls(os.open(root, cls._FLAGS | _O_CLOEXEC))

    def lstat(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def names(self) -> list[str]:
        return os.listdir(self._fd)

    def child(self, name: str, *, create: bool) -> _PosixDirectory:
        found = self.lstat(name)
        _refuse_an_alias(self, name, found)
        if found is None:
            if not create:
                raise FileNotFoundError(errno.ENOENT, "no such directory", name)
            try:
                os.mkdir(name, 0o700, dir_fd=self._fd)
            except FileExistsError:
                # Created between the stat and here; the no-follow open below still checks it.
                pass
        try:
            fd = os.open(name, self._FLAGS | _O_CLOEXEC, dir_fd=self._fd)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK) or (
                (again := self.lstat(name)) is not None and _kind(again) is EntryKind.SYMLINK
            ):
                raise _linked(name) from error
            if error.errno == errno.ENOTDIR:
                raise NotADirectoryError(errno.ENOTDIR, "not a directory", name) from error
            raise
        return _PosixDirectory(fd)

    def close(self) -> None:
        os.close(self._fd)

    def __enter__(self) -> _PosixDirectory:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self, name: str, max_bytes: int) -> bytes:
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=self._fd)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                raise OSError(errno.ELOOP, "refusing to read a link", name) from error
            raise
        try:
            if not stat_module.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "not a regular file", name)
            return _read_capped(fd, name, max_bytes)
        finally:
            os.close(fd)

    def write(self, name: str, content: bytes) -> None:
        part = f"{_PART_PREFIX}{secrets.token_hex(8)}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
        fd = os.open(part, flags, 0o600, dir_fd=self._fd)
        try:
            try:
                _write_all(fd, content)
            finally:
                os.close(fd)
            # A rename replaces the name and never writes through what stood there.
            os.replace(part, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
        except BaseException:
            try:
                os.unlink(part, dir_fd=self._fd)
            except OSError:
                # Best effort: the write's own failure is the one to report.
                pass
            raise


class _WindowsDirectory:
    """A directory addressed by path, with every component checked for a reparse point."""

    def __init__(self, path: str) -> None:
        self._path = path

    @classmethod
    def open_root(cls, root: Path) -> _WindowsDirectory:
        return cls(str(root))

    def _at(self, name: str) -> str:
        return os.path.join(self._path, name)

    def lstat(self, name: str) -> os.stat_result | None:
        try:
            return os.lstat(self._at(name))
        except FileNotFoundError:
            return None

    def names(self) -> list[str]:
        return os.listdir(self._path)

    def child(self, name: str, *, create: bool) -> _WindowsDirectory:
        found = self.lstat(name)
        _refuse_an_alias(self, name, found)
        if found is None:
            if not create:
                raise FileNotFoundError(errno.ENOENT, "no such directory", name)
            os.makedirs(self._at(name), exist_ok=True)
            found = self.lstat(name)
            if found is None:
                raise FileNotFoundError(errno.ENOENT, "directory vanished after creation", name)
        kind = _kind(found)
        if kind is EntryKind.SYMLINK:
            raise _linked(name)
        if kind is not EntryKind.DIRECTORY:
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", name)
        return _WindowsDirectory(self._at(name))

    def close(self) -> None:
        return None

    def __enter__(self) -> _WindowsDirectory:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self, name: str, max_bytes: int) -> bytes:
        found = self.lstat(name)
        if found is None:
            raise FileNotFoundError(errno.ENOENT, "no such file", name)
        if _kind(found) is EntryKind.SYMLINK:
            raise OSError(errno.ELOOP, "refusing to read a link", name)
        if _kind(found) is not EntryKind.FILE:
            raise OSError(errno.EINVAL, "not a regular file", name)
        fd = os.open(self._at(name), os.O_RDONLY | _O_BINARY)
        try:
            return _read_capped(fd, name, max_bytes)
        finally:
            os.close(fd)

    def write(self, name: str, content: bytes) -> None:
        part = self._at(f"{_PART_PREFIX}{secrets.token_hex(8)}.part")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY)
        try:
            try:
                _write_all(fd, content)
            finally:
                os.close(fd)
            os.replace(part, self._at(name))
        except BaseException:
            try:
                os.unlink(part)
            except OSError:
                # Best effort: the write's own failure is the one to report.
                pass
            raise


def _read_capped(fd: int, name: str, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(fd, min(_READ_CHUNK, max_bytes + 1 - total)):
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise SandboxTransferCapExceeded(
                f"{name!r} is larger than the {max_bytes}-byte cap on this read"
            )
    return b"".join(chunks)


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        view = view[os.write(fd, view) :]


class WorkspacePlane:
    """Maps guest paths under ``guest_root`` onto the host directory ``host_root``.

    A guest path above ``guest_root`` is reported as a directory: the plane never acts there,
    and the guest's own view of it decides nothing the plane does.  A path beside or below
    anything else is refused as outside the plane's reach.
    """

    def __init__(self, host_root: Path, guest_root: str, *, windows: bool = _WINDOWS) -> None:
        self._host_root = host_root
        self._guest_root = posixpath.normpath(guest_root)
        self._directory: type[_PosixDirectory] | type[_WindowsDirectory] = (
            _WindowsDirectory if windows else _PosixDirectory
        )
        self._windows = windows

    @property
    def guest_root(self) -> str:
        return self._guest_root

    @property
    def host_root(self) -> Path:
        return self._host_root

    def parts(self, guest_path: str) -> tuple[str, ...] | None:
        """The components under the root, or ``None`` for a path above it."""
        guest = posixpath.normpath(guest_path)
        relative = guest_path_relative_to(guest, self._guest_root)
        if relative is None:
            if guest_path_relative_to(self._guest_root, guest) is not None:
                return None
            raise ValueError(
                f"{guest_path!r} is outside {self._guest_root!r}, the only guest directory this "
                "backend's file plane reaches"
            )
        parts = tuple(relative.split("/")) if relative else ()
        refuse_names_the_host_changes(parts, windows=self._windows)
        return parts

    def _walk(self, parts: Sequence[str], *, create: bool = False) -> _Directory:
        directory: _Directory = self._directory.open_root(self._host_root)
        for name in parts:
            with directory:
                child = directory.child(name, create=create)
            directory = child
        return directory

    def _leaf(self, guest_path: str, *, create: bool = False) -> tuple[_Directory, str]:
        parts = self.parts(guest_path)
        if not parts:
            raise ValueError(f"{guest_path!r} names no entry inside the workspace")
        return self._walk(parts[:-1], create=create), parts[-1]

    def lstat(self, guest_path: str) -> SandboxEntry | None:
        """A no-follow stat for the confinement helpers; ``path`` is the guest path."""
        parts = self.parts(guest_path)
        if parts is None or not parts:
            return SandboxEntry(path=guest_path, kind=EntryKind.DIRECTORY, size_bytes=None)
        try:
            directory = self._walk(parts[:-1])
        except FileNotFoundError:
            return None
        with directory:
            found = directory.lstat(parts[-1])
            _refuse_an_alias(directory, parts[-1], found)
        if found is None:
            return None
        kind = _kind(found)
        size = found.st_size if kind is EntryKind.FILE else None
        return SandboxEntry(path=guest_path, kind=kind, size_bytes=size)

    def read(self, guest_path: str, max_bytes: int) -> bytes:
        directory, name = self._leaf(guest_path)
        with directory:
            return directory.read(name, max_bytes)

    def write(self, guest_path: str, content: bytes) -> None:
        directory, name = self._leaf(guest_path, create=True)
        with directory:
            _refuse_an_alias(directory, name, directory.lstat(name))
            directory.write(name, content)

    def make_directories(self, guest_path: str) -> None:
        parts = self.parts(guest_path)
        if parts:
            with self._walk(parts, create=True):
                pass

    def list(self, guest_path: str) -> list[tuple[str, SandboxEntry]]:
        """Each child's name and entry; the entry's ``path`` is left for the caller to set."""
        parts = self.parts(guest_path) or ()
        with self._walk(parts) as directory:
            listed: list[tuple[str, SandboxEntry]] = []
            for name in sorted(directory.names()):
                found = directory.lstat(name)
                if found is None:
                    continue
                kind = _kind(found)
                size = found.st_size if kind is EntryKind.FILE else None
                listed.append((name, SandboxEntry(path=name, kind=kind, size_bytes=size)))
            return listed
