"""Guest-path arithmetic for the protocol's one path grammar, shared by kinds and backends,
the confinement rule written on top of it, and the tar-header stat a container backend reads
off its copy stream.

A guest path is POSIX whatever the host runs, so everything here goes through ``posixpath``,
never ``os.path``, and a backslash is refused rather than read as a separator.  For a *host*
filesystem path this module is the wrong answer — use :meth:`pathlib.Path.resolve` and
:meth:`pathlib.Path.is_relative_to`, which know the host's grammar and follow its symlinks.

Confinement has two halves and one function each, and the names are worth keeping straight.
**The file name check** is :func:`confine_resolve_guest_path`: text arithmetic over a whole
guest path — join, normalise, refuse anything resolving outside — and it cannot see a symlink,
which is why the other exists.  It is not :func:`~maf_sandbox.portable_file_name`, which
rewrites the *segments* of a name for a hostile filesystem and confines nothing.
**The filesystem path check** is :func:`refuse_symlinked_ancestors`: it looks at the guest's
real filesystem, one directory at a time from the root down, and refuses a path whose ancestors
are not real directories.

**Reach for a bundle rather than for either half.**  Four of them pair the two checks, one per
confinement policy, and the file surface's five methods map onto them:
:func:`confine_resolve_guest_write_path` for ``write_file``,
:func:`confine_resolve_guest_read_path` for ``stat_file`` and ``read_file`` — one policy for
both, since leaving the final component to the caller is what lets a stat describe a link and a
read refuse one on kind — :func:`confine_resolve_guest_list_path` for ``list_dir`` and
:func:`confine_resolve_guest_delete_path` for ``remove``.  What separates them is what each does
about the final component and about the working directory itself, and that belongs to the policy
rather than to a keyword argument: a caller that picked the wrong bundle named the wrong method
out loud, where one that omitted an argument would have got a default in silence.

**The prefix says what a function hands back.**  ``confine_resolve_*`` returns the resolved
guest path or raises; ``refuse_*`` returns nothing and raises; ``tar_header_from_block`` and
``sandbox_entry_from_tar_header`` name their returns the same way.  The one that answers a
``bool`` does the naming differently: :func:`path_ancestors_are_host_owned` is named as the
fact it states rather than as a question, because a caller reads its answer as permission to
raise authority rather than as an outcome to await.
"""

from __future__ import annotations

import inspect
import posixpath
import tarfile
import warnings
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any

from ._protocol import EntryKind, SandboxEntry

__all__ = [
    "confine_guest_path",
    "confine_guest_write_path",
    "confine_resolve_guest_delete_path",
    "confine_resolve_guest_list_path",
    "confine_resolve_guest_path",
    "confine_resolve_guest_read_path",
    "confine_resolve_guest_write_path",
    "guest_directory_chain",
    "guest_path_and_ancestors",
    "guest_path_relative_to",
    "path_ancestors_are_host_owned",
    "refuse_symlinked_ancestors",
    "refuse_symlinked_parents",
    "sandbox_entry_from_tar_header",
    "tar_header_from_block",
]


def confine_resolve_guest_path(path: str, working_directory: str) -> str:
    """The file name check: POSIX-join ``path`` onto ``working_directory`` and refuse an escape.

    Raises a bare :class:`ValueError`, which ``maf_sandbox`` translates into
    ``SandboxOutputNotConfined`` only on the pull surface — a kind calling this directly gets
    the ``ValueError``.
    """
    if "\\" in path:
        raise ValueError(f"path {path!r} contains a backslash, which is not a valid separator")
    base = posixpath.normpath(working_directory)
    resolved = posixpath.normpath(posixpath.join(base, path))
    if guest_path_relative_to(resolved, base) is None:
        raise ValueError(f"path {path!r} resolves outside working directory {working_directory!r}")
    return resolved


async def confine_resolve_guest_write_path(
    stat: Callable[[str], Awaitable[SandboxEntry | None]],
    path: str,
    working_directory: str,
) -> str:
    """Confine a write using an unconfined, no-follow stat; refuse a link at the leaf."""
    resolved = confine_resolve_guest_path(path, working_directory)
    if resolved == posixpath.normpath(working_directory):
        raise ValueError(f"refusing to write over the working directory itself: {resolved!r}")
    await refuse_symlinked_ancestors(stat, resolved, working_directory)
    entry = await stat(resolved)
    if entry is not None and entry.kind is EntryKind.SYMLINK:
        raise ValueError(
            f"{resolved!r} is a link, so writing to it would land the bytes wherever it points, "
            f"outside working directory {working_directory!r}"
        )
    return resolved


async def confine_resolve_guest_read_path(
    stat: Callable[[str], Awaitable[SandboxEntry | None]],
    path: str,
    working_directory: str,
) -> str:
    """Confine a stat or a read using an unconfined, no-follow stat; the leaf is the caller's.

    Left there deliberately: a stat describes a link, which is how a caller learns it is one,
    and a read refuses it on the kind it got back.
    """
    resolved = confine_resolve_guest_path(path, working_directory)
    await refuse_symlinked_ancestors(stat, resolved, working_directory)
    return resolved


async def confine_resolve_guest_list_path(
    stat: Callable[[str], Awaitable[SandboxEntry | None]],
    path: str,
    working_directory: str,
) -> str:
    """Confine an enumeration using an unconfined, no-follow stat; the directory itself included.

    An enumeration passes through a link as readily as a read does, so the directory named here
    is checked as well as its ancestors.
    """
    resolved = confine_resolve_guest_path(path, working_directory)
    await refuse_symlinked_ancestors(stat, resolved, working_directory, include_self=True)
    return resolved


async def confine_resolve_guest_delete_path(
    stat: Callable[[str], Awaitable[SandboxEntry | None]],
    path: str,
    working_directory: str,
) -> str:
    """Confine a removal using an unconfined, no-follow stat; the target is never resolved.

    A link named here is the thing being unlinked, so the check stops above it — a removal is
    the one operation on the file surface that must not resolve its own final component.
    """
    resolved = confine_resolve_guest_path(path, working_directory)
    if resolved == posixpath.normpath(working_directory):
        raise ValueError(f"refusing to remove the working directory itself: {resolved!r}")
    await refuse_symlinked_ancestors(stat, resolved, working_directory)
    return resolved


def tar_header_from_block(block: bytes) -> tarfile.TarInfo:
    """Parse the first 512-byte tar block of a container ``cp`` stream into a tar header.

    The block is what a container engine streams before any content byte, and it carries the
    size, the entry-type flag and the link target — everything a stat needs and how a backend
    stats a guest with no shell. The decoding arguments are pinned here rather than spelled per
    caller, so two backends cannot drift apart on how a header's names are read.
    """
    return tarfile.TarInfo.frombuf(block, encoding="utf-8", errors="surrogateescape")


def sandbox_entry_from_tar_header(info: tarfile.TarInfo, rel_path: str) -> SandboxEntry:
    """Classify a tar header into a :class:`~maf_sandbox.SandboxEntry` at ``rel_path``.

    A regular file maps to :data:`~maf_sandbox.EntryKind.FILE` with its size, a directory to
    :data:`~maf_sandbox.EntryKind.DIRECTORY`, a symlink to :data:`~maf_sandbox.EntryKind.SYMLINK`
    and every other entry — a hard link, a fifo, a device node — to
    :data:`~maf_sandbox.EntryKind.OTHER`. Non-regular entries answer a ``None`` size, so a
    caller refuses them before ever reading a byte.

    A **hard** link stays :data:`~maf_sandbox.EntryKind.OTHER`: it names an inode rather than a
    path, so it is not a way out of the working directory, and it is refused as non-regular
    regardless.

    An extended header (GNU or PAX, what a writer emits ahead of an entry whose name exceeds
    100 bytes) is not classified: it maps to :data:`~maf_sandbox.EntryKind.OTHER` like any
    other non-regular block, and the caller refuses it. A caller that can reach long names
    must skip the extended block itself — the pair of backends this serves names entries short
    enough to stat, and extending the protocol to carry them is a change to the pull surface,
    not to this classifier.
    """
    if info.isreg():
        return SandboxEntry(path=rel_path, kind=EntryKind.FILE, size_bytes=info.size)
    if info.isdir():
        return SandboxEntry(path=rel_path, kind=EntryKind.DIRECTORY, size_bytes=None)
    if info.issym():
        return SandboxEntry(path=rel_path, kind=EntryKind.SYMLINK, size_bytes=None)
    return SandboxEntry(path=rel_path, kind=EntryKind.OTHER, size_bytes=None)


def guest_path_relative_to(path: str, base: str) -> str | None:
    """``path`` relative to ``base``, or ``None`` when it does not sit inside ``base``.

    Both are normalised first, so a caller using this as its own containment check cannot be
    escaped by a ``..`` the string comparison would otherwise carry:
    ``/maf-sandbox/work/../etc`` is outside ``/maf-sandbox/work`` and answers ``None``.
    Comparison is against ``base + "/"`` rather than ``base``, so a sibling sharing a string
    prefix — ``/maf-sandbox/work/sub2`` under ``/maf-sandbox/work/sub`` — is not read as a
    descendant.
    """
    resolved = posixpath.normpath(path)
    root = posixpath.normpath(base)
    if resolved == root:
        return ""
    prefix = root if root.endswith("/") else root + "/"
    if not resolved.startswith(prefix):
        return None
    return resolved[len(prefix) :]


def guest_path_and_ancestors(guest_path: str, working_directory: str) -> tuple[str, ...]:
    """Every directory from the filesystem root down to ``guest_path``, outermost first.

    The filesystem path check starts *above* ``working_directory`` rather than at it, because a
    nested work dir has ancestors the guest can replace, and stat-ing only the work dir
    follows straight through them.  ``guest_path`` must already be confined.
    """
    base = posixpath.normpath(working_directory)
    path_and_ancestors: list[str] = []
    so_far = ""
    for segment in (s for s in base.split("/") if s):
        so_far = f"{so_far}/{segment}"
        path_and_ancestors.append(so_far)
    relative = guest_path_relative_to(guest_path, base)
    if relative:
        for segment in relative.split("/"):
            path_and_ancestors.append(
                posixpath.join(path_and_ancestors[-1] if path_and_ancestors else "/", segment)
            )
    return tuple(path_and_ancestors)


async def refuse_symlinked_ancestors(
    stat: Callable[[str], Awaitable[SandboxEntry | None]],
    guest_path: str,
    working_directory: str,
    *,
    include_self: bool = False,
) -> None:
    """The filesystem path check: refuse ``guest_path`` unless every directory above it is real.

    A link found among them raises :class:`ValueError`, the same refusal an unconfined path
    gets; any other non-directory raises :class:`NotADirectoryError`, because a fifo where a
    directory was expected is the guest tripping rather than escaping.  A component that is not
    there ends the check — there is nothing below it to reach.

    Three things ``stat`` must be, or this answers about the wrong filesystem: **unconfined**,
    since the ancestors include the working directory's own; **no-follow**, since a stat
    that resolves a link describes its target and hides the escape; and **not answered by the
    guest** wherever the backend has any other mechanism, since a workload asked to describe
    its own filesystem can answer falsely — a root guest replaces ``test`` in its own image.
    A backend with no other mechanism says so in its README, and the repository's own
    ``tests/test_confinement_stat_source.py`` is what holds it to that.  ``include_self``
    extends the check to ``guest_path`` itself, which an enumeration needs — a listing passes
    through a link as readily as a read does.
    """
    deepest = guest_path if include_self else posixpath.dirname(guest_path)
    for directory in guest_path_and_ancestors(deepest, working_directory):
        entry = await stat(directory)
        if entry is None:
            return
        if entry.kind is EntryKind.SYMLINK:
            raise ValueError(
                f"{directory!r} is a link rather than a real directory, so a path through "
                f"it does not stay inside working directory {working_directory!r}"
            )
        if entry.kind is not EntryKind.DIRECTORY:
            raise NotADirectoryError(f"{directory!r} is not a directory")


def path_ancestors_are_host_owned(
    walked: Mapping[str, tuple[int, int]], *, empty_means_host_owned: bool
) -> bool:
    """Whether every directory on the walked path is root's and writable by nobody else.

    The reach rule: a removal may run with more authority than the guest program had only
    where nothing on the path was that program's to replace.  ``walked`` holds ``(uid,
    mode)`` per component the caller's check on the path collected, and the caller must hand
    every component the rule is to bind — a missing one is a question for the caller, not a
    pass.

    An empty mapping is not decided here: it can mean nothing lies above the working
    directory, or that the walk reached nothing, and the caller names what that means by
    passing ``empty_means_host_owned``.  Running a *stat* as root buys reach, not trust — the
    binary answering is the image's either way.  Running a *removal* as root is what needs
    licensing, and this is what licenses it.
    """
    if not walked:
        return empty_means_host_owned
    return all(uid == 0 and not mode & 0o022 for uid, mode in walked.values())


def _warn_renamed(old: str, new: str) -> None:
    """The notice a caller still on the old spelling gets, once per call."""
    warnings.warn(
        f"maf_sandbox.paths.{old} is deprecated and is removed in the next minor; use {new}.",
        DeprecationWarning,
        stacklevel=3,
    )


# The spellings these four had before the rename, served for one minor. Importing one must not
# warn — a backend that imports it would fail under ``-W error`` — so the notice is on the call.


def confine_guest_path(path: str, working_directory: str) -> str:
    """Deprecated. Use :func:`confine_resolve_guest_path`."""
    _warn_renamed("confine_guest_path", "confine_resolve_guest_path")
    return confine_resolve_guest_path(path, working_directory)


@inspect.markcoroutinefunction
def confine_guest_write_path(
    stat: Callable[[str], Awaitable[SandboxEntry | None]],
    path: str,
    working_directory: str,
) -> Coroutine[Any, Any, str]:
    """Deprecated. Use :func:`confine_resolve_guest_write_path`.

    Deliberately not ``async``: an ``async def`` body runs only once the event loop has taken
    the coroutine, so the warning would be attributed to ``asyncio`` rather than to the caller.
    Warning here and handing back the replacement's coroutine keeps the notice at the call
    site, and ``await`` on the result is unchanged.

    The marker is what keeps that invisible.  This spelling *was* an ``async def``, so a caller
    dispatching on :func:`inspect.iscoroutinefunction` would otherwise read the shim as
    synchronous and stop awaiting it — a break during the one minor that exists to avoid one.
    """
    _warn_renamed("confine_guest_write_path", "confine_resolve_guest_write_path")
    return confine_resolve_guest_write_path(stat, path, working_directory)


def guest_directory_chain(guest_path: str, working_directory: str) -> tuple[str, ...]:
    """Deprecated. Use :func:`guest_path_and_ancestors`."""
    _warn_renamed("guest_directory_chain", "guest_path_and_ancestors")
    return guest_path_and_ancestors(guest_path, working_directory)


@inspect.markcoroutinefunction
def refuse_symlinked_parents(
    stat: Callable[[str], Awaitable[SandboxEntry | None]],
    guest_path: str,
    working_directory: str,
    *,
    include_self: bool = False,
) -> Coroutine[Any, Any, None]:
    """Deprecated. Use :func:`refuse_symlinked_ancestors`.

    Not ``async``, and marked, for the reasons :func:`confine_guest_write_path` gives.
    """
    _warn_renamed("refuse_symlinked_parents", "refuse_symlinked_ancestors")
    return refuse_symlinked_ancestors(
        stat, guest_path, working_directory, include_self=include_self
    )
