"""Widening the dependents' ceiling so the next core minor is admitted before it exists.

`scripts/widen_dependents_ceiling.py` runs after a core publish and is the automated half of
RELEASING.md's step 1. Its rewriting is a pure function of a pyproject's text, so it is tested
here rather than discovered on a release.

Two properties carry the rest: the target is **two** minors up, because a ceiling of `<0.8`
excludes 0.8.0 itself and admitting the next release is the entire point; and it only ever
widens, so re-running on a patch — which the release workflow will do — changes nothing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "widen_dependents_ceiling.py"
)
_spec = importlib.util.spec_from_file_location("widen_dependents_ceiling", _SCRIPT)
assert _spec and _spec.loader
widen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(widen)


def _pyproject(constraint: str, name: str = "maf-sandbox-acas") -> str:
    return f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = ["{constraint}"]\n'


class TestTheTarget:
    """Two minors up, so the release after this one is admitted."""

    @pytest.mark.parametrize(
        ("released", "expected"),
        [
            ((0, 7, 0), (0, 9)),
            ((0, 7, 3), (0, 9)),
            ((0, 9, 0), (0, 11)),
            ((1, 2, 0), (1, 4)),
        ],
    )
    def test_it_admits_the_next_minor(
        self, released: tuple[int, ...], expected: tuple[int, ...]
    ):
        assert widen.target_ceiling(released) == expected

    def test_one_minor_up_would_exclude_the_release_it_is_for(self):
        # The mistake this guards: <0.8 does not admit 0.8.0.
        target = widen.target_ceiling((0, 7, 0))
        assert target > (0, 8), (
            "a ceiling of <0.8 excludes the 0.8.0 it is meant to admit"
        )


class TestWidening:
    def test_a_narrow_ceiling_moves_and_the_floor_does_not(self):
        text, did = widen.widen(_pyproject("maf-sandbox>=0.7.0,<0.8"), (0, 7, 0))
        assert did
        assert "maf-sandbox>=0.7.0,<0.9" in text

    def test_a_ceiling_already_at_the_target_is_left_alone(self):
        text, did = widen.widen(_pyproject("maf-sandbox>=0.7.0,<0.9"), (0, 7, 0))
        assert not did
        assert "maf-sandbox>=0.7.0,<0.9" in text

    def test_it_never_narrows(self):
        _, did = widen.widen(_pyproject("maf-sandbox>=0.7.0,<1.5"), (0, 7, 0))
        assert not did

    def test_a_patch_release_changes_nothing_once_widened(self):
        once, _ = widen.widen(_pyproject("maf-sandbox>=0.7.0,<0.8"), (0, 7, 0))
        twice, did = widen.widen(once, (0, 7, 1))
        assert not did and twice == once

    def test_a_constraint_it_cannot_read_is_left_for_run_to_refuse(self):
        _, did = widen.widen(_pyproject("maf-sandbox>=0.7.0"), (0, 7, 0))
        assert not did


class TestOverATree:
    def test_it_widens_the_dependents_and_not_the_core(self, tmp_path: Path):
        core = tmp_path / "packages" / "maf-sandbox"
        core.mkdir(parents=True)
        (core / "pyproject.toml").write_text(
            '[project]\nname = "maf-sandbox"\nversion = "0.7.0"\ndependencies = []\n',
            "utf-8",
        )
        for name in ("maf-sandbox-acas", "maf-sandbox-wslc"):
            package = tmp_path / "packages" / name
            package.mkdir(parents=True)
            (package / "pyproject.toml").write_text(
                _pyproject("maf-sandbox>=0.7.0,<0.8", name), "utf-8"
            )

        changed = widen.run("0.7.0", tmp_path)

        assert {p.parent.name for p in changed} == {
            "maf-sandbox-acas",
            "maf-sandbox-wslc",
        }
        assert "dependencies = []" in (core / "pyproject.toml").read_text("utf-8")
        for name in ("maf-sandbox-acas", "maf-sandbox-wslc"):
            text = (tmp_path / "packages" / name / "pyproject.toml").read_text("utf-8")
            assert "maf-sandbox>=0.7.0,<0.9" in text

    def test_an_unreadable_constraint_stops_the_step(self, tmp_path: Path):
        package = tmp_path / "packages" / "maf-sandbox-acas"
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            _pyproject("maf-sandbox>=0.7.0"), "utf-8"
        )
        with pytest.raises(SystemExit):
            widen.run("0.7.0", tmp_path)

    def test_a_package_that_does_not_depend_on_the_core_is_skipped(
        self, tmp_path: Path
    ):
        package = tmp_path / "packages" / "unrelated"
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            '[project]\nname = "unrelated"\nversion = "1.0"\ndependencies = ["httpx>=1"]\n',
            "utf-8",
        )
        assert widen.run("0.7.0", tmp_path) == []
