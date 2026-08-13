"""Guest-path arithmetic for the protocol's one path grammar, shared by kinds and backends,
and the one confinement rule written on top of it that a backend cannot express without a stat.

A guest path is POSIX whatever the host runs, so everything here goes through ``posixpath``,
never ``os.path``, and a backslash is refused rather than read as a separator.  For a *host*
filesystem path this module is the wrong answer — use :meth:`pathlib.Path.resolve` and
:meth:`pathlib.Path.is_relative_to`, which know the host's grammar and follow its symlinks.
"""

from __future__ import annotations

import posixpath
from collections.abc import Awaitable, Callable

from ._protocol import EntryKind, SandboxEntry

__all__ = [
    "confine_guest_path",
    "guest_directory_chain",
    "guest_path_relative_to",
    "refuse_symlinked_parents",
]


def confine_guest_path(path: str, working_directory: str) -> str:
    """POSIX-join ``path`` onto ``working_directory`` and refuse anything that escapes it.

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


def guest_path_relative_to(path: str, base: str) -> str | None:
    """``path`` relative to ``base``, or ``None`` when it does not sit inside ``base``.

    Both are normalised first, so a caller using this as its own containment check cannot be
    walked out of by a ``..`` the string comparison would otherwise carry: ``/maf-sandbox/work/../etc``
    is outside ``/maf-sandbox/work`` and answers ``None``.  Comparison is against ``base + "/"`` rather
    than ``base``, so a sibling sharing a string prefix — ``/maf-sandbox/work/sub2`` under ``/maf-sandbox/work/sub``
    — is not read as a descendant.
    """
    resolved = posixpath.normpath(path)
    root = posixpath.normpath(base)
    if resolved == root:
        return ""
    prefix = root if root.endswith("/") else root + "/"
    if not resolved.startswith(prefix):
        return None
    return resolved[len(prefix) :]


def guest_directory_chain(guest_path: str, working_directory: str) -> tuple[str, ...]:
    """Every directory from the filesystem root down to ``guest_path``, outermost first.

    The walk starts *above* ``working_directory`` rather than at it, because a nested work dir
    has ancestors the guest can replace and stat-ing only the work dir follows straight through
    them.  ``guest_path`` must already be confined.
    """
    base = posixpath.normpath(working_directory)
    chain: list[str] = []
    walked = ""
    for segment in (s for s in base.split("/") if s):
        walked = f"{walked}/{segment}"
        chain.append(walked)
    relative = guest_path_relative_to(guest_path, base)
    if relative:
        for segment in relative.split("/"):
            chain.append(posixpath.join(chain[-1] if chain else "/", segment))
    return tuple(chain)


async def refuse_symlinked_parents(
    stat: Callable[[str], Awaitable[SandboxEntry | None]],
    guest_path: str,
    working_directory: str,
    *,
    include_self: bool = False,
) -> None:
    """Refuse ``guest_path`` unless every directory above it is a real one.

    A link found in the chain raises :class:`ValueError`, the same refusal an unconfined path
    gets; any other non-directory raises :class:`NotADirectoryError`, because a fifo where a
    directory was expected is the guest tripping rather than escaping.  A component that is not
    there ends the walk — there is nothing below it to reach.

    Two things ``stat`` must be, or this answers about the wrong filesystem: **unconfined**,
    since the chain covers the working directory's own ancestors, and **no-follow**, since a
    stat that resolves a link describes its target and hides the escape.  ``include_self``
    extends the walk to ``guest_path`` itself, which an enumeration needs — a listing passes
    through a link as readily as a read does.
    """
    deepest = guest_path if include_self else posixpath.dirname(guest_path)
    for directory in guest_directory_chain(deepest, working_directory):
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
