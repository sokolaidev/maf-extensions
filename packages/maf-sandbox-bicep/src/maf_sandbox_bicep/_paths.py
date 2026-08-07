"""The injection-pinning guard.

``exec`` takes a **command string**, not an argv list, so the usual "build argv yourself"
rule becomes: a fixed template with exactly one interpolation, and that interpolation
validated against the workspace listing first.  This module is that validation.
"""

from __future__ import annotations

import re

__all__ = ["safe_workspace_path"]

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

    >>> safe_workspace_path("main.bicep", ["main.bicep"], "/work")
    '/work/main.bicep'
    >>> safe_workspace_path("main.bicep; rm -rf /", ["main.bicep; rm -rf /"], "/work") is None
    True
    """
    if not name or _UNSAFE_CHARS.search(name):
        return None
    # Normalise: strip leading / and ./ so "main.bicep" and "./main.bicep" both match.
    normalised = name.lstrip("/").removeprefix("./")
    # Reject path traversal regardless of workspace membership.
    if ".." in normalised.split("/"):
        return None
    workspace_normalised = {f.lstrip("/").removeprefix("./") for f in workspace_files}
    if normalised not in workspace_normalised:
        return None
    return f"{work_dir}/{normalised}"
