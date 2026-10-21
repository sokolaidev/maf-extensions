"""Resolve explicit file manifests without exposing names in refusal messages."""

import posixpath
import re

from maf_sandbox import ListedFile

from ._spec import ShellDialect, ShellFlavor

_RESERVED = {".shellcheckrc", "shellcheckrc", "psscriptanalyzersettings.psd1"}


def normalize(name: object) -> str:
    """Accept bounded store-relative paths, including leading-hyphen basenames."""
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 512
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", name)
        or name.startswith("/")
        or name.endswith("/")
        or ".." in name.split("/")
        or any(part.lower() in _RESERVED for part in name.split("/"))
    ):
        raise ValueError("The manifest contains an unsupported or reserved path.")
    path = posixpath.normpath(name)
    if path == ".":
        raise ValueError("Every manifest entry must name a file.")
    return path


def resolve_manifest(
    files: list[str], listing: list[ListedFile], flavor: ShellFlavor
) -> list[tuple[str, ListedFile]]:
    """Keep listing objects for provenance and refuse ambiguous normalized names."""
    paths = [normalize(name) for name in files]
    if len(set(paths)) != len(paths):
        raise ValueError("The manifest contains duplicate destinations.")
    indexed: dict[str, ListedFile] = {}
    for item in listing:
        try:
            path = normalize(item.name)
        except ValueError:
            continue
        if path in indexed:
            raise ValueError("The file listing contains ambiguous normalized paths.")
        indexed[path] = item
    for path in paths:
        suffix = posixpath.splitext(path)[1].lower()
        allowed = {".ps1", ".psm1"} if flavor == ShellFlavor.PWSH else {".sh", ".bash", ".dash", ""}
        if suffix not in allowed:
            raise ValueError("The manifest contains a file unsupported by this shell flavor.")
        if path not in indexed:
            raise ValueError("A manifest file is absent from this tool's file listing.")
    return [(path, indexed[path]) for path in paths]


def dialect_for(path: str, text: str, default: ShellDialect) -> ShellDialect:
    """Select a fixed grammar without executing or forwarding the shebang."""
    suffix = posixpath.splitext(path)[1].lower()
    named = {".bash": ShellDialect.BASH, ".dash": ShellDialect.DASH}.get(suffix)
    if text.startswith("#!"):
        words = text.splitlines()[0][2:].strip().split()
        if len(words) == 2 and words[0] == "/usr/bin/env":
            executable = words[1]
        elif len(words) == 1 and words[0].startswith("/"):
            executable = posixpath.basename(words[0])
        else:
            raise ValueError("The shebang is not a supported fixed shell declaration.")
        try:
            dialect = ShellDialect(executable)
        except ValueError:
            raise ValueError("The shebang names an unsupported shell dialect.") from None
        if named is not None and named != dialect:
            raise ValueError("The file extension and shebang declare conflicting dialects.")
        return dialect
    if not suffix:
        raise ValueError("An extensionless shell file requires a supported shebang.")
    return named or default
