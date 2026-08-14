"""The release-time helper that sets the dependents' maf-sandbox range.

`scripts/set_dependents_range.py` is what `release-please.yml` runs once `maf-sandbox` has
published. It replaces two scripts that each owned one end of the same string and opened
competing pull requests (#195), so the first thing pinned here is that **one edit carries both
bounds**.

The rest is what made the pair safe, kept: the ceiling only widens, the floor moves only for a
candidate — a dependent whose ceiling admits the version and whose floor is a minor behind,
which is a shape rather than evidence the package uses it — the floor is judged against the
ceiling as it was rather than as this run leaves it, and a constraint the pattern cannot read
stops the step instead of silently no-opping.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "set_dependents_range.py"
_spec = importlib.util.spec_from_file_location("set_dependents_range", _SCRIPT)
assert _spec and _spec.loader
ranges = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ranges)

FLOOR, CEILING = ranges.FLOOR, ranges.CEILING
BOTH = frozenset({FLOOR, CEILING})


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
    def test_it_admits_the_next_minor(self, released: tuple[int, ...], expected: tuple[int, ...]):
        assert ranges.target_ceiling(released) == expected

    def test_one_minor_up_would_exclude_the_release_it_is_for(self):
        # The mistake this guards: <0.8 does not admit 0.8.0.
        assert ranges.target_ceiling((0, 7, 0)) > (0, 8), (
            "a ceiling of <0.8 excludes the 0.8.0 it is meant to admit"
        )


class TestParseConstraint:
    def test_reads_the_floor_and_ceiling(self):
        floor, ceiling = ranges.parse_constraint("maf-sandbox>=0.2.0,<0.3")
        assert floor == (0, 2, 0)
        assert ceiling == (0, 3)

    def test_a_constraint_without_the_expected_shape_is_none(self):
        assert ranges.parse_constraint("maf-sandbox") is None
        assert ranges.parse_constraint("maf-sandbox>=0.2.0") is None


class TestBothBoundsInOneEdit:
    """#195: two writers on one line meant the second merge reverted the first."""

    def test_a_core_minor_moves_the_floor_and_the_ceiling_together(self):
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.9"), (0, 8, 0))
        assert moved == BOTH
        assert "maf-sandbox>=0.8.0,<0.10" in text

    def test_neither_end_is_left_behind_by_the_other(self):
        # The failure mode: an edit that widens and forgets the floor, or vice versa, is what
        # produced two pull requests that each reverted half of the other.
        text, _ = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.9"), (0, 8, 0))
        assert "0.7.0" not in text
        assert "<0.9," not in text and ',<0.9"' not in text


class TestTheCeiling:
    def test_a_narrow_ceiling_widens(self):
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.8"), (0, 7, 0))
        assert moved == frozenset({CEILING})
        assert "maf-sandbox>=0.7.0,<0.9" in text

    def test_a_ceiling_already_at_the_target_is_left_alone(self):
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.9"), (0, 7, 0))
        assert moved == frozenset()
        assert "maf-sandbox>=0.7.0,<0.9" in text

    def test_it_never_narrows(self):
        _, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<1.5"), (0, 7, 0))
        assert moved == frozenset()


class TestTheFloor:
    def test_a_new_minor_the_ceiling_admits_moves_the_floor(self):
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.1.0,<0.3"), (0, 2, 0))
        assert FLOOR in moved
        assert "maf-sandbox>=0.2.0," in text

    def test_a_patch_within_the_floor_minor_changes_nothing(self):
        text = _pyproject("maf-sandbox>=0.2.0,<0.4")
        new_text, moved = ranges.set_range(text, (0, 2, 3))
        assert moved == frozenset()
        assert new_text == text

    def test_a_floor_already_at_the_release_changes_nothing(self):
        _, moved = ranges.set_range(_pyproject("maf-sandbox>=0.3.0,<0.5"), (0, 3, 0))
        assert moved == frozenset()

    def test_it_is_judged_against_the_ceiling_as_it_was_not_as_this_leaves_it(self):
        # <0.8 excludes 0.9.0, so this dependent has not adopted it and its floor must not
        # move — even though the same edit widens that ceiling to <0.11. Widening authorising
        # the bump the old ceiling refused is the regression this run could have introduced.
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.8"), (0, 9, 0))
        assert moved == frozenset({CEILING})
        assert "maf-sandbox>=0.7.0,<0.11" in text


class TestSpelling:
    def test_an_unmoved_ceiling_keeps_its_own_spelling(self):
        # <0.4 already admits the next minor after 0.2.0, so only the floor moves and the
        # ceiling must come through as it was written.
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.1.0,<0.4"), (0, 2, 0))
        assert moved == frozenset({FLOOR})
        assert "maf-sandbox>=0.2.0,<0.4" in text  # not <0.4.0

    def test_an_unmoved_floor_keeps_its_own_spelling(self):
        # A two-component floor is not this script's house style, but rewriting one it was
        # not asked to touch would put a spurious hunk in front of a reviewer.
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7,<0.8"), (0, 7, 0))
        assert moved == frozenset({CEILING})
        assert "maf-sandbox>=0.7,<0.9" in text  # not >=0.7.0

    def test_a_constraint_it_cannot_read_is_left_for_plan_to_refuse(self):
        _, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0"), (0, 7, 0))
        assert moved == frozenset()


class TestTheTitle:
    def test_both_bounds(self):
        assert ranges.title("0.8.0", BOTH) == (
            "fix: require maf-sandbox 0.8.0 and admit 0.9 in the dependents' range"
        )

    def test_the_ceiling_alone(self):
        assert ranges.title("0.8.0", frozenset({CEILING})) == (
            "fix: admit maf-sandbox 0.9 in the dependents' range"
        )

    def test_the_floor_alone(self):
        assert ranges.title("0.8.0", frozenset({FLOOR})) == (
            "fix: require maf-sandbox 0.8.0 in the packages that use it"
        )

    def test_nothing_moved_has_no_title(self):
        assert ranges.title("0.8.0", frozenset()) == ""

    @pytest.mark.parametrize("moved", [BOTH, frozenset({CEILING}), frozenset({FLOOR})])
    def test_every_title_releases_something(self, moved: frozenset[str]):
        # chore: and ci: release nothing here, and an unpublished range is worth nothing.
        assert ranges.title("0.8.0", moved).startswith("fix: ")


class TestOverATree:
    def _write(self, tmp_path: Path, name: str, dependency: str) -> Path:
        package = tmp_path / "packages" / name
        package.mkdir(parents=True)
        path = package / "pyproject.toml"
        path.write_text(f'[project]\nname = "{name}"\ndependencies = ["{dependency}"]\n', "utf-8")
        return path

    def test_it_edits_the_dependents_and_not_the_core(self, tmp_path: Path):
        core = self._write(tmp_path, "maf-sandbox", "anyio>=4")
        dependents = [
            self._write(tmp_path, name, "maf-sandbox>=0.7.0,<0.9")
            for name in ("maf-sandbox-acas", "maf-sandbox-wslc")
        ]

        changed = ranges.run("0.8.0", tmp_path)

        assert changed == sorted(dependents)
        assert "anyio>=4" in core.read_text("utf-8")
        for path in dependents:
            assert "maf-sandbox>=0.8.0,<0.10" in path.read_text("utf-8")

    def test_plan_changes_nothing_on_disk(self, tmp_path: Path):
        path = self._write(tmp_path, "dep-a", "maf-sandbox>=0.7.0,<0.9")
        before = path.read_text("utf-8")

        planned = ranges.plan("0.8.0", tmp_path)

        assert [p for p, _, _ in planned] == [path]
        assert planned[0][2] == BOTH
        assert path.read_text("utf-8") == before

    def test_a_second_run_is_a_clean_no_op(self, tmp_path: Path):
        path = self._write(tmp_path, "dep-a", "maf-sandbox>=0.7.0,<0.9")
        assert ranges.run("0.8.0", tmp_path) == [path]
        after = path.read_text("utf-8")
        assert ranges.run("0.8.0", tmp_path) == []
        assert path.read_text("utf-8") == after

    def test_a_patch_release_moves_nothing(self, tmp_path: Path):
        self._write(tmp_path, "dep-a", "maf-sandbox>=0.8.0,<0.10")
        assert ranges.run("0.8.1", tmp_path) == []

    def test_a_dependent_whose_constraint_drifted_fails_loudly(self, tmp_path: Path):
        self._write(tmp_path, "dep-a", "maf-sandbox @ git+https://example.invalid/x")
        with pytest.raises(SystemExit):
            ranges.run("0.8.0", tmp_path)

    def test_a_package_that_does_not_depend_on_the_core_is_skipped(self, tmp_path: Path):
        self._write(tmp_path, "unrelated", "httpx>=1")
        assert ranges.run("0.8.0", tmp_path) == []

    def test_single_quoted_toml_is_read_not_silently_skipped(self, tmp_path: Path):
        # Single quotes are valid TOML; a quote-specific matcher would skip this and pass green.
        package = tmp_path / "packages" / "dep-a"
        package.mkdir(parents=True)
        path = package / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'dep-a'\ndependencies = ['maf-sandbox>=0.7.0,<0.9']\n",
            "utf-8",
        )
        assert ranges.run("0.8.0", tmp_path) == [path]
        assert "maf-sandbox>=0.8.0,<0.10" in path.read_text("utf-8")

    def test_a_dependent_on_only_a_sibling_is_not_mistaken_for_a_base_dependent(
        self, tmp_path: Path
    ):
        # maf-sandbox-acas is a sibling, not the base — this package must be skipped, not
        # demanded to carry a maf-sandbox>=X,<Y constraint it has no reason to.
        self._write(tmp_path, "dep-a", "maf-sandbox-acas>=0.2.0,<0.3")
        assert ranges.run("0.8.0", tmp_path) == []

    def test_a_dependent_on_both_edits_the_base_and_ignores_the_sibling(self, tmp_path: Path):
        package = tmp_path / "packages" / "dep-a"
        package.mkdir(parents=True)
        path = package / "pyproject.toml"
        path.write_text(
            '[project]\nname = "dep-a"\n'
            'dependencies = ["maf-sandbox-acas>=0.1.0,<0.3", "maf-sandbox>=0.7.0,<0.9"]\n',
            "utf-8",
        )

        assert ranges.run("0.8.0", tmp_path) == [path]

        text = path.read_text("utf-8")
        assert "maf-sandbox>=0.8.0,<0.10" in text
        assert "maf-sandbox-acas>=0.1.0,<0.3" in text  # the sibling, exactly as it was
