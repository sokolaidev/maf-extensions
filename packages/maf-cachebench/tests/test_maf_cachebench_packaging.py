"""What the package owes its installer: the experimental notice and an honest dependency list."""

from __future__ import annotations

import ast
import importlib
import sys
import tomllib
import warnings
from pathlib import Path

import pytest

import maf_cachebench

_SRC = Path(maf_cachebench.__file__).resolve().parent


def _package_modules() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _imported_top_levels(path: Path) -> set[str]:
    """Every top-level name a module imports, from any depth, including TYPE_CHECKING blocks."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return names


def _declared_import_names() -> set[str] | None:
    """The import names of pyproject's dependencies, or None outside a source checkout."""
    pyproject = _SRC.parent.parent / "pyproject.toml"
    if not pyproject.is_file():
        return None
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    requirements = list(project["dependencies"])
    # The provider clients are imported lazily, inside the builder that needs them, so an
    # extra's packages are declared imports too.
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    names: set[str] = set()
    for requirement in requirements:
        distribution = requirement.split(">")[0].split("<")[0].split("=")[0].split("[")[0]
        names.add(distribution.strip().replace("-", "_"))
    # Distributions whose import name is not their distribution name.
    if "agent_framework_core" in names:
        names.add("agent_framework")
    if "azure_identity" in names:
        names.add("azure")
    return names


class TestExperimentalWarning:
    def test_is_a_user_warning_not_a_future_or_deprecation_warning(self):
        category = maf_cachebench.MafCachebenchExperimentalWarning
        assert issubclass(category, UserWarning)
        assert not issubclass(category, DeprecationWarning)
        assert not issubclass(category, FutureWarning)

    def test_emitted_on_import(self):
        with pytest.warns(UserWarning, match=r"maf_cachebench is experimental"):
            importlib.reload(maf_cachebench)

    def test_suppressible_by_category(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("error")
            warnings.filterwarnings(
                "ignore", category=maf_cachebench.MafCachebenchExperimentalWarning
            )
            warnings.warn(
                "maf_cachebench is experimental and may change or be removed in future "
                "versions without notice.",
                category=maf_cachebench.MafCachebenchExperimentalWarning,
                stacklevel=1,
            )
        assert caught == []


class TestOnlyDeclaredDependencies:
    """Every module imports only the standard library, itself, or a declared dependency.

    The workspace running this suite has every sibling package importable, so an undeclared
    import resolves here and fails only for whoever installs the published wheel alone. The
    source is read rather than imported, so an import inside a ``TYPE_CHECKING`` block counts.
    """

    def test_sources_exist(self):
        assert len(_package_modules()) >= 15

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        declared = _declared_import_names()
        if declared is None:
            pytest.skip("pyproject.toml is not beside the installed package")
        allowed = set(sys.stdlib_module_names) | declared | {"maf_cachebench"}
        offenders = [
            f"{path.name}: import {name}"
            for path in _package_modules()
            for name in sorted(_imported_top_levels(path))
            if name not in allowed
        ]
        assert offenders == [], offenders

    def test_the_strategies_are_imported_from_their_own_package(self):
        """Nothing here re-implements or vendors a strategy; it measures maf_compaction's."""
        importers = [
            path.name
            for path in _package_modules()
            if "maf_compaction" in _imported_top_levels(path)
        ]
        assert importers, (
            "no module imports maf_compaction, so nothing here measures its strategies"
        )
