"""The injection-pinning guard.

``exec`` takes a **command string**, not an argv list, so the usual "build argv yourself"
rule becomes: a fixed template with exactly one interpolation, and that interpolation
validated against the file store listing first.  This module is that validation.
"""

from __future__ import annotations

import re
from typing import Literal

__all__ = ["PathRejection", "resolve_listed_path", "safe_listed_path"]

#: ``"unsafe"`` is the injection guard; ``"missing"`` is a wiring problem, not a security one.
PathRejection = Literal["unsafe", "missing"]

# Characters that must not appear in a store-relative file name used as a shell token.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._\-/]")


def safe_listed_path(name: str, listing: list[str], work_dir: str) -> str | None:
    """Return the in-sandbox absolute path for ``name``, or ``None`` if it is not safe.

    Rejects any name containing characters outside the ASCII-alphanumeric-plus-safe-punct
    set, any path containing a ``..`` component, and any name not present in
    ``listing``.

    Membership in the listing is checked **as well as** the character class, not instead of
    it: a file can be created with a hostile name, so "it is really in the file store" is not
    evidence that it is safe to interpolate into a shell string.

    Prefer :func:`resolve_listed_path`: this collapses two unrelated reasons into
    ``None``, so a caller can only describe the failure in a way that is half wrong.

    >>> safe_listed_path("main.bicep", ["main.bicep"], "/maf-sandbox/work")
    '/maf-sandbox/work/main.bicep'
    >>> safe_listed_path("main.bicep; rm -rf /", ["main.bicep; rm -rf /"],
    ...                      "/maf-sandbox/work") is None
    True
    """
    path, _, _ = resolve_listed_path(name, listing, work_dir)
    return path


def resolve_listed_path(
    name: str, listing: list[str], work_dir: str
) -> tuple[str | None, str | None, PathRejection | None]:
    """Resolve ``name`` to ``(sandbox_path, listing_key, rejection)``.

    ``listing_key`` is the entry from ``listing`` that matched, which is what the
    store is keyed by — ``name`` may spell it differently and not read back.

    Membership is checked **as well as** the character class: a file can be created with a
    hostile name. The character class runs first, so a hostile name reads as hostile
    whether or not it is listed.

    >>> resolve_listed_path("./main.bicep", ["main.bicep"], "/maf-sandbox/work")
    ('/maf-sandbox/work/main.bicep', 'main.bicep', None)
    >>> resolve_listed_path("main.bicep", [], "/maf-sandbox/work")
    (None, None, 'missing')
    >>> resolve_listed_path("main.bicep; rm -rf /", ["main.bicep; rm -rf /"], "/maf-sandbox/work")
    (None, None, 'unsafe')
    """
    if not name or _UNSAFE_CHARS.search(name):
        return None, None, "unsafe"
    # Strip leading / and ./ so "main.bicep" and "./main.bicep" both match.
    normalised = name.lstrip("/").removeprefix("./")
    if ".." in normalised.split("/"):
        return None, None, "unsafe"
    by_normalised = {f.lstrip("/").removeprefix("./"): f for f in listing}
    listing_key = by_normalised.get(normalised)
    if listing_key is None:
        return None, None, "missing"
    return f"{work_dir}/{normalised}", listing_key, None
