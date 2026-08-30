"""Guest-path arithmetic for the protocol's one path grammar, shared by kinds and backends,
and the one confinement rule written on top of it that a backend cannot express without a stat.

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

**A backend calls neither of those directly.**  Four bundles pair them, one per method of the
file surface — :func:`confine_resolve_guest_write_path`, :func:`confine_resolve_guest_read_path`,
:func:`confine_resolve_guest_list_path` and :func:`confine_resolve_guest_delete_path`.  What
separates them is what each does about the final component and about the working directory
itself, and that belongs to the method rather than to a keyword argument: a caller that picked
the wrong bundle named the wrong method out loud, where one that omitted an argument would have
got a default in silence.

**The prefix says what a function hands back.**  ``confine_resolve_*`` returns the resolved
guest path or raises; ``refuse_*`` returns nothing and raises.  Nothing here answers a
``bool``, so a name in the shape of a predicate would be read as one and is not used.
"""

from __future__ import annotations

import inspect
import posixpath
import warnings
from collections.abc import Awaitable, Callable, Coroutine
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
    "refuse_symlinked_ancestors",
    "refuse_symlinked_parents",
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
