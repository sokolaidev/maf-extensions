"""The injection-pinning guard.

``exec`` takes a **command string**, not an argv list, so the usual "build argv yourself"
rule becomes: a fixed template with exactly one interpolation, and that interpolation
validated against the workspace listing first.  This module is that validation.
"""

from __future__ import annotations

import re
from typing import Literal

__all__ = ["PathRejection", "resolve_workspace_path", "safe_workspace_path"]

#: ``"unsafe"`` is the injection guard; ``"missing"`` is a wiring problem, not a security one.
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

    Prefer :func:`resolve_workspace_path`: this collapses two unrelated reasons into
    ``None``, so a caller can only describe the failure in a way that is half wrong.

    >>> safe_workspace_path("main.bicep", ["main.bicep"], "/work")
    '/work/main.bicep'
    >>> safe_workspace_path("main.bicep; rm -rf /", ["main.bicep; rm -rf /"], "/work") is None
    True
    """
    path, _, _ = resolve_workspace_path(name, workspace_files, work_dir)
    return path


def resolve_workspace_path(
    name: str, workspace_files: list[str], work_dir: str
) -> tuple[str | None, str | None, PathRejection | None]:
    """Resolve ``name`` to ``(sandbox_path, listing_key, rejection)``.

    ``listing_key`` is the entry from ``workspace_files`` that matched, which is what the
    store is keyed by — ``name`` may spell it differently and not read back.

    Membership is checked **as well as** the character class: a file can be created with a
    hostile name. The character class runs first, so a hostile name reads as hostile
    whether or not it is listed.

    >>> resolve_workspace_path("./main.bicep", ["main.bicep"], "/work")
    ('/work/main.bicep', 'main.bicep', None)
    >>> resolve_workspace_path("main.bicep", [], "/work")
    (None, None, 'missing')
    >>> resolve_workspace_path("main.bicep; rm -rf /", ["main.bicep; rm -rf /"], "/work")
    (None, None, 'unsafe')
    """
    if not name or _UNSAFE_CHARS.search(name):
        return None, None, "unsafe"
    # Strip leading / and ./ so "main.bicep" and "./main.bicep" both match.
    normalised = name.lstrip("/").removeprefix("./")
    if ".." in normalised.split("/"):
        return None, None, "unsafe"
    by_normalised = {f.lstrip("/").removeprefix("./"): f for f in workspace_files}
    listing_key = by_normalised.get(normalised)
    if listing_key is None:
        return None, None, "missing"
    return f"{work_dir}/{normalised}", listing_key, None
