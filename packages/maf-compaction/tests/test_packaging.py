"""What the package owes its installer: the experimental notice and an honest dependency list."""

from __future__ import annotations

import ast
import importlib
import sys
import tomllib
import warnings
from pathlib import Path

import pytest

import maf_compaction

_SRC = Path(maf_compaction.__file__).resolve().parent


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
    names: set[str] = set()
    for requirement in project["dependencies"]:
        distribution = requirement.split(">")[0].split("<")[0].split("=")[0].split("[")[0]
        names.add(distribution.strip().replace("-", "_"))
    # agent-framework-core installs the `agent_framework` package.
    if "agent_framework_core" in names:
        names.add("agent_framework")
    return names


class TestExperimentalWarning:
    def test_is_a_user_warning_not_a_future_or_deprecation_warning(self):
        category = maf_compaction.MafCompactionExperimentalWarning
        assert issubclass(category, UserWarning)
        assert not issubclass(category, DeprecationWarning)
        assert not issubclass(category, FutureWarning)

    def test_emitted_on_import(self):
        with pytest.warns(UserWarning, match=r"maf_compaction is experimental"):
            importlib.reload(maf_compaction)

    def test_suppressible_by_category(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("error")
            warnings.filterwarnings(
                "ignore", category=maf_compaction.MafCompactionExperimentalWarning
            )
            warnings.warn(
                "maf_compaction is experimental and may change or be removed in future "
                "versions without notice.",
                category=maf_compaction.MafCompactionExperimentalWarning,
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
        assert len(_package_modules()) >= 5

    def test_every_module_only_imports_what_it_is_declared_to_need(self):
        declared = _declared_import_names()
        if declared is None:
            pytest.skip("pyproject.toml is not beside the installed package")
        allowed = set(sys.stdlib_module_names) | declared | {"maf_compaction"}
        offenders = [
            f"{path.name}: import {name}"
            for path in _package_modules()
            for name in sorted(_imported_top_levels(path))
            if name not in allowed
        ]
        assert offenders == [], offenders

    def test_the_benchmark_is_never_imported(self):
        """The strategies are what the benchmark measures, never its dependant."""
        offenders = [
            path.name
            for path in _package_modules()
            if "maf_cachebench" in _imported_top_levels(path)
        ]
        assert offenders == []
