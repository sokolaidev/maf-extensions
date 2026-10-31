"""Dependency-only PRs must qualify their consumers without rebuilding unrelated images."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import image_dependency_selection as dependencies  # noqa: E402
from select_image_security import PROFILES  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.fixture
def graph(monkeypatch):
    names = {name for roots in dependencies.ROOTS.values() for name in roots}
    names.update({"pyyaml", "ruff", "maf-sandbox-tui", "renderer"})
    before = {
        "version": 1,
        "package": [{"name": name, "version": "1.0", "dependencies": []} for name in sorted(names)],
    }
    next(p for p in before["package"] if p["name"] == "maf-sandbox-document")["dependencies"] = [
        {"name": "renderer"}
    ]
    after = copy.deepcopy(before)

    def document(root, revision, path):
        assert path == "uv.lock"
        return copy.deepcopy(before if revision == "before" else after)

    monkeypatch.setattr(dependencies, "document", document)
    return before, after


def selected():
    return dependencies.affected_dependencies(ROOT, "before", "after", {"uv.lock"}, PROFILES)


def test_every_profile_declares_its_build_and_qualification_roots():
    assert set(dependencies.ROOTS) == set(PROFILES)


@pytest.mark.parametrize("name", ["ruff", "maf-sandbox-tui"])
def test_unrelated_dependency_update_does_not_rebuild_images(graph, name):
    next(p for p in graph[1]["package"] if p["name"] == name)["version"] = "2.0"
    assert selected() == set()


@pytest.mark.parametrize(
    "field,value",
    [("version", "2.0"), ("wheels", [{"hash": "changed"}]), ("source", {"registry": "other"})],
)
def test_transitive_runtime_changes_select_the_consuming_image(graph, field, value):
    next(p for p in graph[1]["package"] if p["name"] == "renderer")[field] = value
    assert selected() == {"document"}


def test_shared_qualification_dependency_selects_all_profiles(graph):
    next(p for p in graph[1]["package"] if p["name"] == "pyyaml")["version"] = "2.0"
    assert selected() == set(PROFILES)


def test_global_resolution_policy_selects_all_profiles(graph):
    graph[1]["requires-python"] = ">=3.14"
    assert selected() == set(PROFILES)


def test_all_optional_dependencies_and_marker_variants_are_traversed(graph):
    for lock in graph:
        next(p for p in lock["package"] if p["name"] == "maf-sandbox-shell")[
            "optional-dependencies"
        ] = {"extra": [{"name": "renderer", "marker": "sys_platform == 'linux'"}]}
        lock["package"].append({"name": "renderer", "version": "2.0", "dependencies": []})
    graph[1]["package"][-1]["version"] = "3.0"
    assert selected() == {"document", "shell"}


def test_missing_dependency_is_not_treated_as_unchanged(graph):
    graph[1]["package"] = [p for p in graph[1]["package"] if p["name"] != "renderer"]
    with pytest.raises(KeyError):
        selected()


@pytest.mark.parametrize(
    "name,expected", [("maf-sandbox-document", {"document"}), ("maf-sandbox-tui", set())]
)
def test_package_metadata_changes_select_its_consumers(graph, monkeypatch, name, expected):
    read = dependencies.document
    monkeypatch.setattr(
        dependencies,
        "document",
        lambda root, ref, path: (
            read(root, ref, path) if path == "uv.lock" else {"project": {"name": name}}
        ),
    )
    assert (
        dependencies.affected_dependencies(
            ROOT, "before", "after", {f"packages/{name}/pyproject.toml"}, PROFILES
        )
        == expected
    )


def test_dev_constraint_change_without_runtime_resolution_change_skips_images(graph, monkeypatch):
    read = dependencies.document
    monkeypatch.setattr(
        dependencies,
        "document",
        lambda root, ref, path: (
            read(root, ref, path) if path == "uv.lock" else {"dependency-groups": {"dev": [ref]}}
        ),
    )
    assert (
        dependencies.affected_dependencies(ROOT, "before", "after", {"pyproject.toml"}, PROFILES)
        == set()
    )
