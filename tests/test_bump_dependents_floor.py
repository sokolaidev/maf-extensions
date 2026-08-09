"""The release-time helper that raises the dependents' maf-sandbox floor after a release.

`scripts/bump_dependents_floor.py` is what `release-please.yml` runs once `maf-sandbox` has
published, to open the floor-raise pull request a person used to remember by hand. These tests
pin the two things that make it safe: it moves the floor only for a dependent that has actually
adopted the new version (its ceiling admits it and its floor is a minor behind), and it fails
loudly rather than silently no-op when the constraint it expects to edit is not there.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "bump_dependents_floor.py"

_spec = importlib.util.spec_from_file_location("bump_dependents_floor", _SCRIPT)
assert _spec and _spec.loader
bump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump)


def _pyproject(dependency: str) -> str:
    return f'[project]\nname = "x"\ndependencies = [\n    "{dependency}",\n    "anyio>=4",\n]\n'


class TestParseConstraint:
    def test_reads_the_floor_and_ceiling(self):
        floor, ceiling = bump.parse_constraint("maf-sandbox>=0.2.0,<0.3")
        assert floor == (0, 2, 0)
        assert ceiling == (0, 3)

    def test_a_constraint_without_the_expected_shape_is_none(self):
        assert bump.parse_constraint("maf-sandbox") is None
        assert bump.parse_constraint("maf-sandbox>=0.2.0") is None


class TestBumpFloor:
    def test_a_new_minor_the_ceiling_admits_moves_the_floor_and_keeps_the_ceiling(self):
        text = _pyproject("maf-sandbox>=0.1.0,<0.3")
        new_text, changed = bump.bump_floor(text, (0, 2, 0))
        assert changed
        assert "maf-sandbox>=0.2.0,<0.3" in new_text

    def test_a_patch_within_the_floor_minor_changes_nothing(self):
        text = _pyproject("maf-sandbox>=0.2.0,<0.3")
        new_text, changed = bump.bump_floor(text, (0, 2, 3))
        assert not changed
        assert text == new_text

    def test_a_version_the_ceiling_excludes_changes_nothing(self):
        # 0.3.0 is not admitted by <0.3, so this dependent has not adopted it.
        text = _pyproject("maf-sandbox>=0.2.0,<0.3")
        _, changed = bump.bump_floor(text, (0, 3, 0))
        assert not changed

    def test_a_floor_already_at_the_release_changes_nothing(self):
        text = _pyproject("maf-sandbox>=0.3.0,<0.4")
        _, changed = bump.bump_floor(text, (0, 3, 0))
        assert not changed

    def test_the_ceiling_is_preserved_exactly(self):
        text = _pyproject("maf-sandbox>=0.1.0,<0.3")
        new_text, _ = bump.bump_floor(text, (0, 2, 0))
        assert ",<0.3" in new_text
        assert ",<0.4" not in new_text


class TestMain:
    def _write(self, tmp_path: Path, name: str, dependency: str) -> Path:
        pkg = tmp_path / "packages" / name
        pkg.mkdir(parents=True)
        path = pkg / "pyproject.toml"
        path.write_text(
            f'[project]\nname = "{name}"\ndependencies = ["{dependency}"]\n', "utf-8"
        )
        return path

    def test_it_bumps_an_adopting_dependent_and_leaves_the_others(self, tmp_path):
        adopting = self._write(tmp_path, "dep-a", "maf-sandbox>=0.1.0,<0.3")
        not_yet = self._write(
            tmp_path, "dep-b", "maf-sandbox>=0.2.0,<0.3"
        )  # patch, no move
        self._write(tmp_path, "maf-sandbox", "anyio>=4")  # not a dependent of itself

        changed = bump.run("0.2.0", tmp_path)

        assert changed == [adopting]
        assert "maf-sandbox>=0.2.0,<0.3" in adopting.read_text("utf-8")
        assert "maf-sandbox>=0.2.0,<0.3" in not_yet.read_text("utf-8")  # untouched

    def test_a_dependent_whose_constraint_drifted_fails_loudly(self, tmp_path):
        # A maf-sandbox dependency the pattern cannot read is the silent-no-op bullet 2 warns of.
        self._write(tmp_path, "dep-a", "maf-sandbox @ git+https://example.invalid/x")
        with pytest.raises(SystemExit):
            bump.run("0.2.0", tmp_path)

    def test_nothing_to_adopt_is_a_clean_no_op(self, tmp_path):
        self._write(tmp_path, "dep-a", "maf-sandbox>=0.2.0,<0.3")
        assert bump.run("0.2.1", tmp_path) == []

    def test_a_dependent_on_only_a_sibling_is_not_mistaken_for_a_base_dependent(
        self, tmp_path
    ):
        # maf-sandbox-aca is a sibling, not the base — this package must be skipped, not
        # demanded to carry a maf-sandbox>=X,<Y constraint it has no reason to.
        self._write(tmp_path, "dep-a", "maf-sandbox-aca>=0.2.0,<0.3")
        assert bump.run("0.3.0", tmp_path) == []

    def test_a_dependent_on_both_bumps_the_base_and_ignores_the_sibling(self, tmp_path):
        pkg = tmp_path / "packages" / "dep-a"
        pkg.mkdir(parents=True)
        path = pkg / "pyproject.toml"
        path.write_text(
            '[project]\nname = "dep-a"\n'
            'dependencies = ["maf-sandbox-aca>=0.1.0,<0.3", "maf-sandbox>=0.1.0,<0.3"]\n',
            "utf-8",
        )
        assert bump.run("0.2.0", tmp_path) == [path]
        text = path.read_text("utf-8")
        assert "maf-sandbox>=0.2.0,<0.3" in text
        assert (
            "maf-sandbox-aca>=0.1.0,<0.3" in text
        )  # the sibling is left exactly as it was
