"""Resolve an explicit project manifest without dropping configuration siblings."""

import posixpath
import re

from maf_sandbox import ListedFile

from ._spec import TerraformEngine

_CONFIG = (".tf", ".tf.json", ".tofu", ".tofu.json")
_RESERVED_PARTS = {
    ".terraform",
    ".git",
    ".runner",
    ".ssh",
    ".aws",
    ".azure",
    ".config",
    "__pycache__",
}
_RESERVED_NAMES = {
    ".terraformrc",
    ".tofurc",
    "terraform.rc",
    "tofu.rc",
    "credentials.tfrc.json",
    ".env",
}


def normalize(name: str, *, directory: bool = False) -> str:
    """Accept bounded relative POSIX paths and normalize harmless dot components."""
    if (
        not name
        or len(name) > 512
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", name)
        or name.startswith("/")
        or ".." in name.split("/")
        or (not directory and name.endswith("/"))
    ):
        raise ValueError("Paths must be relative, use [A-Za-z0-9._/-], and contain no '..'.")
    path = posixpath.normpath(name)
    if path == "." and not directory:
        raise ValueError("A file path must name a file.")
    return path


def _allowed(path: str, engine: TerraformEngine) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return not (
        set(parts) & _RESERVED_PARTS
        or name in _RESERVED_NAMES
        or ".tfstate" in name
        or name.endswith((".tfplan", ".tfvars", ".tfvars.json", ".exe", ".so", ".dll"))
        or name.startswith(("terraform-provider-", ".env."))
        or (name.startswith(".") and path.endswith(_CONFIG))
        or (engine == "terraform" and path.endswith((".tofu", ".tofu.json")))
    )


def resolve_manifest(
    files: list[str], root: str, listing: list[ListedFile], engine: TerraformEngine
) -> tuple[str, list[tuple[str, ListedFile]]]:
    """Keep original listing entries for provenance and reject ambiguous or partial sets.

    Refusals deliberately contain no store or argument names: either can carry hidden content.
    Completeness is checked against the visible listing, not files the host never shared.
    """
    root = normalize(root, directory=True)
    paths = [normalize(name) for name in files]
    if len(set(paths)) != len(paths):
        raise ValueError("The file manifest contains duplicate destinations.")
    if not all(_allowed(path, engine) for path in paths):
        raise ValueError("The manifest contains an unsupported engine file or a reserved path.")
    indexed: dict[str, ListedFile] = {}
    for item in listing:
        try:
            path = normalize(item.name)
        except ValueError:
            continue
        if path in indexed:
            raise ValueError("The file listing contains ambiguous normalized paths.")
        indexed[path] = item
    if any(path not in indexed for path in paths):
        raise ValueError("A manifest file is absent from this tool's file listing.")
    directories = {posixpath.dirname(path) or "." for path in paths}
    selected = set(paths)
    if any(
        path.endswith(_CONFIG)
        and (posixpath.dirname(path) or ".") in directories
        and path not in selected
        for path in indexed
    ):
        raise ValueError("Include every configuration sibling in each selected module directory.")
    suffixes = _CONFIG if engine == "opentofu" else (".tf", ".tf.json")
    if not any(
        (posixpath.dirname(path) or ".") == root and path.endswith(suffixes) for path in paths
    ):
        raise ValueError(
            "root_module must contain at least one configuration file for this engine."
        )
    return root, [(path, indexed[path]) for path in paths]
