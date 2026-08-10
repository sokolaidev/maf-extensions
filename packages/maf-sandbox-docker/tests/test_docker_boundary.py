"""Dependency discipline: every import must be traceable to a reason.

The workspace running this suite has every sibling package already importable, so a stray
import resolves fine here regardless of what it names — the first sign of trouble is a
downstream consumer who installs the published wheel alone and gets an `ImportError` with no
test pointing at the cause. These scans are that test.
"""

from __future__ import annotations

import pytest

#: A requirement string's distribution name is not always its import name: `maf-sandbox` puts
#: `maf_sandbox` on the path. Anything not listed here is assumed to import under its
#: distribution name with hyphens turned to underscores.
_DISTRIBUTION_TO_IMPORT_NAME = {"maf-sandbox": "maf_sandbox"}


def _package_modules():
    """Every module in the installed `maf_sandbox_docker`, as `{stem: path}`."""
    import pathlib

    import maf_sandbox_docker

    root = pathlib.Path(maf_sandbox_docker.__file__).parent  # type: ignore[arg-type]
    return {path.stem: path for path in root.rglob("*.py")}


def _imported_top_levels(path):
    """The absolute top-level module names imported by the file at `path`."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # relative import — within this package, not a dependency
            top = (node.module or "").split(".")[0]
            if top:
                names.append(top)
    return names


def _declared_import_names():
    """The import names `pyproject.toml` licenses `maf_sandbox_docker` to reach for, or `None`.

    `None` means there is no `pyproject.toml` next to the installed package — an sdist/wheel-only
    install with no source tree alongside it — and the caller must skip rather than let an empty
    dependency list pass the scan below vacuously.
    """
    import pathlib
    import re
    import tomllib

    import maf_sandbox_docker

    root = pathlib.Path(maf_sandbox_docker.__file__).parents[2]  # type: ignore[arg-type]
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        return None

    with pyproject_path.open("rb") as fh:
        requirements = tomllib.load(fh)["project"]["dependencies"]

    names: set[str] = set()
    for requirement in requirements:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match is not None, f"unparseable dependency requirement: {requirement!r}"
        distribution = match.group(0)
        names.add(_DISTRIBUTION_TO_IMPORT_NAME.get(distribution, distribution.replace("-", "_")))
    return names


class TestOnlyDeclaredDependencies:
    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(_package_modules()) >= 3

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        import sys as _sys

        declared = _declared_import_names()
        if declared is None:
            pytest.skip(
                "pyproject.toml is not next to the installed maf_sandbox_docker package — "
                "this check only runs against a source checkout, not an installed-only wheel"
            )

        allowed = set(_sys.stdlib_module_names) | declared | {"maf_sandbox_docker"}
        offenders = [
            f"{path.name}: import {name}"
            for _, path in sorted(_package_modules().items())
            for name in _imported_top_levels(path)
            if name not in allowed
        ]
        assert offenders == [], (
            f"these maf_sandbox_docker modules import something outside the standard library, "
            f"the package itself, and pyproject.toml's declared dependencies: {offenders}. "
            "Either the import is a mistake, or the dependency belongs in pyproject.toml."
        )


class TestNoMafImport:
    """A backend is framework-agnostic: it speaks the protocol, never the host's framework."""

    def test_the_backend_does_not_import_agent_framework(self):
        offenders = sorted(
            path.name
            for path in _package_modules().values()
            if "agent_framework" in _imported_top_levels(path)
        )
        assert offenders == [], (
            f"these maf_sandbox_docker modules import agent_framework: {offenders}. A backend "
            "must be usable by a host that does not run Microsoft Agent Framework at all."
        )
