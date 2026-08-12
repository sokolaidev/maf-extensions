"""Guest-path arithmetic for the protocol's one path grammar, shared by kinds and backends.

A guest path is POSIX whatever the host runs, so everything here goes through ``posixpath``,
never ``os.path``, and a backslash is refused rather than read as a separator.  For a *host*
filesystem path this module is the wrong answer — use :meth:`pathlib.Path.resolve` and
:meth:`pathlib.Path.is_relative_to`, which know the host's grammar and follow its symlinks.
"""

from __future__ import annotations

import posixpath

__all__ = ["confine_guest_path", "guest_directory_chain", "guest_path_relative_to"]


def confine_guest_path(path: str, working_directory: str) -> str:
    """POSIX-join ``path`` onto ``working_directory`` and refuse anything that escapes it.

    Raises a bare :class:`ValueError`, which ``maf_sandbox`` translates into
    ``SandboxOutputNotConfined`` for the caller.
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

    Compares against ``base + "/"``, not ``base``, so a sibling that merely shares a string
    prefix — ``/work/sub2`` under ``/work/sub`` — is not mistaken for a descendant.
    """
    if path == base:
        return ""
    prefix = base if base.endswith("/") else base + "/"
    if not path.startswith(prefix):
        return None
    return path[len(prefix) :]


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
