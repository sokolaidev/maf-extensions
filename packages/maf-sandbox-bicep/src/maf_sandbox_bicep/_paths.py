"""The injection-pinning guard.

``exec`` takes a **command string**, not an argv list, so the usual "build argv yourself"
rule becomes: a fixed template with exactly one interpolation, and that interpolation
validated against the workspace listing first.  This module is that validation.
"""

from __future__ import annotations

import re
from typing import Literal

__all__ = ["PathRejection", "resolve_workspace_path", "safe_workspace_path"]

#: Why a name was refused. ``"unsafe"`` is the injection guard; ``"missing"`` is a name this
#: tool's listing does not contain, which is a wiring problem rather than a security one.
PathRejection = Literal["unsafe", "missing"]

# Characters that must not appear in a workspace-relative file name used as a shell token.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._\-/]")


def safe_workspace_path(name: str, workspace_files: list[str], work_dir: str) -> str | None:
    """Return the in-sandbox absolute path for ``name``, or ``None`` if it is not safe.

    Rejects any name containing characters outside the ASCII-alphanumeric-plus-safe-punct
    set, any path containing a ``..`` component, and any name not present in
    ``workspace_files``.

    Membership in the listing is checked **as well as** the character class, not instead of
    it: a file can be created with a hostile name, so "it is really in the workspace" is not
    evidence that it is safe to interpolate into a shell string.

    Prefer :func:`resolve_workspace_path` in new code: this returns ``None`` for two
    unrelated reasons, and a caller that cannot tell them apart can only describe the
    failure in a way that is wrong half the time.

    >>> safe_workspace_path("main.bicep", ["main.bicep"], "/work")
    '/work/main.bicep'
    >>> safe_workspace_path("main.bicep; rm -rf /", ["main.bicep; rm -rf /"], "/work") is None
    True
    """
    path, _ = resolve_workspace_path(name, workspace_files, work_dir)
    return path


def resolve_workspace_path(
    name: str, workspace_files: list[str], work_dir: str
) -> tuple[str | None, PathRejection | None]:
    """Resolve ``name``, or say *which* rule rejected it.

    The two rejections are not variations of one another and must not be reported as one.
    ``"unsafe"`` is the injection guard refusing a name; ``"missing"`` is a name this tool
    cannot see, which is a wiring or typo problem — most often a host that wired the tool
    over a narrower store than the agent's own read tools, so the agent is told it cannot
    see a file it has already read. Given one message covering both, a model reasonably
    concludes the sandbox is broken and falls back to reviewing the file by eye, which is
    the exact outcome this workload exists to prevent.

    Membership in the listing is still checked **as well as** the character class, not
    instead of it: a file can be created with a hostile name, so "it is really in the
    workspace" is not evidence that it is safe to interpolate into a shell string. Note the
    order below — the character class is applied first, so a hostile name is reported as
    hostile whether or not it is in the listing, and the listing is never quoted back for it.

    >>> resolve_workspace_path("main.bicep", ["main.bicep"], "/work")
    ('/work/main.bicep', None)
    >>> resolve_workspace_path("main.bicep", [], "/work")
    (None, 'missing')
    >>> resolve_workspace_path("main.bicep; rm -rf /", ["main.bicep; rm -rf /"], "/work")
    (None, 'unsafe')
    """
    if not name or _UNSAFE_CHARS.search(name):
        return None, "unsafe"
    # Normalise: strip leading / and ./ so "main.bicep" and "./main.bicep" both match.
    normalised = name.lstrip("/").removeprefix("./")
    # Reject path traversal regardless of workspace membership.
    if ".." in normalised.split("/"):
        return None, "unsafe"
    workspace_normalised = {f.lstrip("/").removeprefix("./") for f in workspace_files}
    if normalised not in workspace_normalised:
        return None, "missing"
    return f"{work_dir}/{normalised}", None
