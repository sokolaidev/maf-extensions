"""The release-time helper that sets the dependents' maf-sandbox range.

`scripts/set_dependents_range.py` is what `release-please.yml` runs once `maf-sandbox` has
published. It replaces two scripts that each owned one end of the same string and opened
competing pull requests (#195), so the first thing pinned here is that **one edit carries both
bounds**.

The rest is what made the pair safe, kept: the ceiling only widens, the floor never lowers, a
patch moves nothing, and a constraint the pattern cannot read stops the step instead of
silently no-opping. Both bounds move to the released minor, so a dependent carries one core
line rather than several.

The samples used to ride the same edit (#343) and now move only under `--samples`, because
merged with the packages' hunk they take the whole set unsatisfiable whenever the core reaches
the index first (0.33.0, 0.34.0).
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

FLOOR, CEILING, SAMPLE = ranges.FLOOR, ranges.CEILING, ranges.SAMPLE_FLOOR
BOTH = frozenset({FLOOR, CEILING})
REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject(constraint: str, name: str = "maf-sandbox-acas") -> str:
    return f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = ["{constraint}"]\n'


def _agent(dependency: str = '"maf-sandbox>=0.7"', docstring: str = "A sample.") -> str:
    """A sample as `uv run` reads one: a module docstring, then a PEP 723 block of TOML.

    The docstring is a parameter because prose is where an unanchored pattern goes wrong: the
    rewrite is `count=1`, so a mention above the block is the match it would find first.
    """
    return (
        f'"""{docstring}"""\n'
        "\n"
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        "# dependencies = [\n"
        '#     "agent-framework-openai",\n'
        '#     "maf-sandbox-acas",\n'
        f"#     {dependency},\n"
        "# ]\n"
        "# ///\n"
        "\n"
        "import maf_sandbox\n"
    )


class TestTheTarget:
    """One minor up: the released line, and no version that does not exist yet."""

    @pytest.mark.parametrize(
        ("released", "expected"),
        [
            ((0, 7, 0), (0, 8)),
            ((0, 7, 3), (0, 8)),
            ((0, 9, 0), (0, 10)),
            ((1, 2, 0), (1, 3)),
        ],
    )
    def test_it_admits_the_released_line(
        self, released: tuple[int, ...], expected: tuple[int, ...]
    ):
        assert ranges.target_ceiling(released) == expected

    def test_it_admits_every_patch_of_the_release(self):
        assert ranges._admits((0, 7, 9), ranges.target_ceiling((0, 7, 0)))

    def test_it_does_not_admit_the_next_minor(self):
        # Admitting a version and being tested against it are the same condition, so reaching
        # the next minor made every breaking core red before it was written.
        assert not ranges._admits((0, 8, 0), ranges.target_ceiling((0, 7, 0)))


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
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.8"), (0, 8, 0))
        assert moved == BOTH
        assert "maf-sandbox>=0.8.0,<0.9" in text

    def test_neither_end_is_left_behind_by_the_other(self):
        # The failure mode: an edit that widens and forgets the floor, or vice versa, is what
        # produced two pull requests that each reverted half of the other.
        text, _ = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.8"), (0, 8, 0))
        assert "0.7.0" not in text
        assert "<0.8," not in text and ',<0.8"' not in text

    def test_the_pair_leaves_one_core_minor_in_range(self):
        # The point of moving both: `>=0.8.0,<0.9` admits the 0.8 line and nothing else.
        text, _ = ranges.set_range(_pyproject("maf-sandbox>=0.6.0,<0.7"), (0, 8, 0))
        floor, ceiling = ranges.parse_constraint(text)
        assert floor[:2] == (0, 8)
        assert ceiling == (0, 9)


class TestTheCeiling:
    def test_a_narrow_ceiling_widens(self):
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.7"), (0, 7, 0))
        assert moved == frozenset({CEILING})
        assert "maf-sandbox>=0.7.0,<0.8" in text

    def test_a_ceiling_already_at_the_target_is_left_alone(self):
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.8"), (0, 7, 0))
        assert moved == frozenset()
        assert "maf-sandbox>=0.7.0,<0.8" in text

    def test_it_never_narrows(self):
        _, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<1.5"), (0, 7, 0))
        assert moved == frozenset()


class TestTheFloor:
    def test_a_new_minor_moves_the_floor(self):
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

    def test_a_ceiling_that_excluded_the_release_does_not_hold_the_floor_back(self):
        # It used to: the floor was judged against the ceiling as it stood, so a dependent two
        # lines behind kept its floor. The suite now tracks one core minor, so both move.
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0,<0.8"), (0, 9, 0))
        assert moved == BOTH
        assert "maf-sandbox>=0.9.0,<0.10" in text


class TestSpelling:
    def test_an_unmoved_ceiling_keeps_its_own_spelling(self):
        # <0.4 already admits the 0.2 line, so only the floor moves and the ceiling must
        # come through as it was written.
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.1.0,<0.4"), (0, 2, 0))
        assert moved == frozenset({FLOOR})
        assert "maf-sandbox>=0.2.0,<0.4" in text  # not <0.4.0

    def test_an_unmoved_floor_keeps_its_own_spelling(self):
        # A two-component floor is not this script's house style, but rewriting one it was
        # not asked to touch would put a spurious hunk in front of a reviewer.
        text, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7,<0.7"), (0, 7, 0))
        assert moved == frozenset({CEILING})
        assert "maf-sandbox>=0.7,<0.8" in text  # not >=0.7.0

    def test_a_constraint_it_cannot_read_is_left_for_plan_to_refuse(self):
        _, moved = ranges.set_range(_pyproject("maf-sandbox>=0.7.0"), (0, 7, 0))
        assert moved == frozenset()


class TestTheSampleFloor:
    """Not a judgement call, unlike a package's: every sample declares the released minor."""

    def test_a_new_minor_moves_it(self):
        text, moved = ranges.set_sample_floor(_agent(), (0, 8, 0))
        assert moved == frozenset({SAMPLE})
        assert '"maf-sandbox>=0.8"' in text

    def test_the_patch_is_dropped(self):
        # A sample declaring >=0.8.3 would claim it needs a patch it has never named, and
        # would make the next patch release rewrite sixteen files to say the same thing.
        text, _ = ranges.set_sample_floor(_agent(), (0, 8, 3))
        assert '"maf-sandbox>=0.8"' in text
        assert "0.8.3" not in text

    def test_a_patch_within_the_declared_minor_moves_nothing(self):
        before = _agent('"maf-sandbox>=0.8"')
        after, moved = ranges.set_sample_floor(before, (0, 8, 3))
        assert moved == frozenset()
        assert after == before

    def test_it_never_lowers(self):
        before = _agent('"maf-sandbox>=0.9"')
        after, moved = ranges.set_sample_floor(before, (0, 8, 0))
        assert moved == frozenset()
        assert after == before

    def test_the_rest_of_the_file_comes_through_byte_for_byte(self):
        # The floor sits inside a comment block that a greedy pattern can run past. Anything
        # this rewrites beyond the version itself is a bug.
        before = _agent()
        after, _ = ranges.set_sample_floor(before, (0, 8, 0))
        assert after == before.replace('"maf-sandbox>=0.7"', '"maf-sandbox>=0.8"')

    def test_prose_quoting_the_dependency_is_not_the_thing_that_moves(self):
        # These samples carry paragraphs above their block, and a docstring quoting the line
        # it is describing is an ordinary thing to write. The rewrite is count=1, so a pattern
        # that accepts prose edits the sentence and leaves the dependency exactly as it was.
        prose = 'The block declares "maf-sandbox>=0.7".'
        after, moved = ranges.set_sample_floor(_agent(docstring=prose), (0, 8, 0))
        assert moved == frozenset({SAMPLE})
        assert prose in after
        assert '#     "maf-sandbox>=0.8",' in after

    def test_a_docstring_showing_the_block_is_not_the_thing_that_moves(self):
        # A sample whose prose quotes its own block, indented inside the docstring. Every line
        # of that copy begins with whitespace rather than `#`, which is the only difference
        # between it and the real thing — and it comes first, so under a pattern without the
        # line anchor `count=1` spends itself on the documentation and the dependency stays.
        shown = (
            'Run it with uv::\n\n    # dependencies = [\n    #     "maf-sandbox>=0.7",\n    # ]\n'
        )
        after, moved = ranges.set_sample_floor(_agent(docstring=shown), (0, 8, 0))
        assert moved == frozenset({SAMPLE})
        assert '    #     "maf-sandbox>=0.7",' in after, "the illustration was rewritten"
        assert '#     "maf-sandbox>=0.8",' in after, "the dependency was left behind"

    def test_a_capped_constraint_is_refused_rather_than_half_rewritten(self):
        # `maf-sandbox>=0.7,<0.9` is the packages' shape, and a sample is not a package: it
        # declares a floor and takes whatever is newest. Matching it would move the floor and
        # leave the ceiling, quietly inventing a range nobody chose.
        before = _agent('"maf-sandbox>=0.7,<0.9"')
        after, moved = ranges.set_sample_floor(before, (0, 8, 0))
        assert moved == frozenset()
        assert after == before

    def test_the_sibling_distribution_is_left_alone(self):
        after, _ = ranges.set_sample_floor(_agent(), (0, 8, 0))
        assert '"maf-sandbox-acas",' in after


class TestTheTwoFileSetsNeverMoveTogether:
    """`--samples` switches the file set, it does not add to it.

    One commit holding both is what took fourteen of fifteen samples unsatisfiable on 0.33.0
    and failed seven live samples on 0.34.0.
    """

    def _sample(self, tmp_path: Path, name: str, text: str) -> Path:
        sample = tmp_path / "samples" / name
        sample.mkdir(parents=True)
        path = sample / "agent.py"
        path.write_text(text, "utf-8")
        return path

    def _package(self, tmp_path: Path, constraint: str) -> Path:
        package = tmp_path / "packages" / "dep-a"
        package.mkdir(parents=True)
        path = package / "pyproject.toml"
        path.write_text(f'[project]\nname = "dep-a"\ndependencies = ["{constraint}"]\n', "utf-8")
        return path

    def test_the_default_run_leaves_a_lagging_sample_where_it_is(self, tmp_path: Path):
        self._package(tmp_path, "maf-sandbox>=0.7.0,<0.8")
        sample = self._sample(tmp_path, "01_a", _agent())
        before = sample.read_text("utf-8")

        changed = ranges.run("0.8.0", tmp_path)

        assert changed, "the package beside it moved, so this is not passing on an empty plan"
        assert sample not in changed
        assert sample.read_text("utf-8") == before

    def test_the_samples_run_leaves_the_packages_where_they_are(self, tmp_path: Path):
        package = self._package(tmp_path, "maf-sandbox>=0.7.0,<0.8")
        before = package.read_text("utf-8")
        sample = self._sample(tmp_path, "01_a", _agent())

        changed = ranges.run("0.8.0", tmp_path, samples=True)

        assert changed == [sample]
        assert package.read_text("utf-8") == before

    def test_a_sample_whose_floor_shape_drifted_fails_loudly(self, tmp_path: Path):
        # The whole reason this raises rather than skips: a step that quietly edits nothing
        # looks exactly like one with nothing to do.
        self._sample(tmp_path, "01_a", _agent('"maf-sandbox"'))
        with pytest.raises(SystemExit):
            ranges.run("0.8.0", tmp_path, samples=True)

    def test_two_dependencies_on_one_line_are_refused_not_half_read(self, tmp_path: Path):
        # Legal TOML the floor pattern cannot read. The danger is the version that skips: a
        # looser probe misses the base behind the sibling and leaves a stale floor behind green.
        self._sample(
            tmp_path,
            "01_a",
            _agent().replace(
                '#     "maf-sandbox-acas",\n#     "maf-sandbox>=0.7",\n',
                '#     "maf-sandbox-acas", "maf-sandbox>=0.7",\n',
            ),
        )
        with pytest.raises(SystemExit):
            ranges.run("0.8.0", tmp_path, samples=True)

    def test_a_sample_naming_only_a_sibling_is_skipped_not_refused(self, tmp_path: Path):
        self._sample(tmp_path, "01_a", _agent('"maf-sandbox-acas>=0.2"'))
        assert ranges.run("0.8.0", tmp_path, samples=True) == []

    def test_a_second_samples_run_is_a_clean_no_op(self, tmp_path: Path):
        path = self._sample(tmp_path, "01_a", _agent())
        assert ranges.run("0.8.0", tmp_path, samples=True) == [path]
        after = path.read_text("utf-8")
        assert ranges.run("0.8.0", tmp_path, samples=True) == []
        assert path.read_text("utf-8") == after

    def test_no_sample_in_this_repository_is_in_the_default_plan(self):
        planned = [path for path, _, _ in ranges.plan("9.9.0", REPO_ROOT)]
        assert planned, "nothing was planned at all; this test is measuring nothing"
        assert not [path for path in planned if "samples" in path.parts], (
            "the samples' floor moves in a pull request of its own, after the dependents publish"
        )

    def test_every_sample_in_this_repository_is_reached_under_the_flag(self):
        # The parser here and the one in tests/test_sample_metadata.py read the same block by
        # different means. This is what keeps them honest about the real files: a sample the
        # script stops recognising would otherwise sail through every fixture above.
        expected = sorted((REPO_ROOT / "samples").glob("[0-9][0-9]_*/agent.py"))
        planned = [path for path, _, _ in ranges.plan("9.9.0", REPO_ROOT, samples=True)]
        assert expected, "no samples found; this test is measuring nothing"
        assert planned == expected


class TestTheTitle:
    def test_both_bounds(self):
        assert ranges.title("0.8.0", BOTH) == (
            "fix: require maf-sandbox 0.8.0 in the dependents, and admit the 0.8 line"
        )

    def test_the_ceiling_alone(self):
        assert ranges.title("0.8.0", frozenset({CEILING})) == (
            "fix: admit the maf-sandbox 0.8 line in the dependents' range"
        )

    def test_the_floor_alone(self):
        assert ranges.title("0.8.0", frozenset({FLOOR})) == (
            "fix: require maf-sandbox 0.8.0 in the packages that use it"
        )

    def test_nothing_moved_has_no_title(self):
        assert ranges.title("0.8.0", frozenset()) == ""

    @pytest.mark.parametrize("moved", [BOTH, frozenset({CEILING})])
    def test_the_ceiling_clause_names_the_released_line(self, moved: frozenset[str]):
        # The bound is `<0.9`, and naming *that* would advertise admitting 0.9 — the claim
        # this policy exists to stop making. The subject says the line it does admit.
        subject = ranges.title("0.8.0", moved)
        assert "0.8 line" in subject
        assert "0.9" not in subject, f"{subject!r} advertises a version that does not exist"

    @pytest.mark.parametrize("moved", [BOTH, frozenset({CEILING}), frozenset({FLOOR})])
    def test_a_title_that_moves_a_package_releases_something(self, moved: frozenset[str]):
        # chore: and ci: release nothing here, and an unpublished range is worth nothing.
        assert ranges.title("0.8.0", moved).startswith("fix: ")

    def test_the_samples_alone_name_the_minor_not_the_release(self):
        # The files say >=0.8; a subject saying 0.8.0 would advertise a claim no file makes.
        assert ranges.title("0.8.0", frozenset({SAMPLE})) == (
            "chore: require maf-sandbox 0.8 in every sample's declared floor"
        )

    def test_a_title_that_moves_only_samples_releases_nothing(self):
        # Attribution is by path and only packages/* is configured, so no type would cut a
        # release here. chore: is what AGENTS.md prescribes outside a package, and it is the
        # one that releases nothing by type rather than by which paths happen to be listed.
        assert ranges.title("0.8.0", frozenset({SAMPLE})).startswith("chore: ")

    @pytest.mark.parametrize(
        "moved", [BOTH, frozenset({CEILING}), frozenset({FLOOR}), frozenset({SAMPLE})]
    )
    def test_every_combination_that_moved_something_is_named(self, moved: frozenset[str]):
        # The gap this closes: an unhandled combination fell through to "" and the workflow
        # refused to commit a change it had already made to the working tree.
        assert ranges.title("0.8.0", moved), f"{sorted(moved)} moved and produced no subject"


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
            self._write(tmp_path, name, "maf-sandbox>=0.7.0,<0.8")
            for name in ("maf-sandbox-acas", "maf-sandbox-wslc")
        ]

        changed = ranges.run("0.8.0", tmp_path)

        assert changed == sorted(dependents)
        assert "anyio>=4" in core.read_text("utf-8")
        for path in dependents:
            assert "maf-sandbox>=0.8.0,<0.9" in path.read_text("utf-8")

    def test_plan_changes_nothing_on_disk(self, tmp_path: Path):
        path = self._write(tmp_path, "dep-a", "maf-sandbox>=0.7.0,<0.8")
        before = path.read_text("utf-8")

        planned = ranges.plan("0.8.0", tmp_path)

        assert [p for p, _, _ in planned] == [path]
        assert planned[0][2] == BOTH
        assert path.read_text("utf-8") == before

    def test_a_second_run_is_a_clean_no_op(self, tmp_path: Path):
        # A dependent whose ceiling already admitted the release, so both bounds settle in the
        # first run and the second has nothing left to reach for.
        path = self._write(tmp_path, "dep-a", "maf-sandbox>=0.7.0,<0.8.5")
        assert ranges.run("0.8.0", tmp_path) == [path]
        after = path.read_text("utf-8")
        assert ranges.run("0.8.0", tmp_path) == []
        assert path.read_text("utf-8") == after

    def test_a_dependent_two_lines_behind_lands_on_the_release(self, tmp_path: Path):
        # Both bounds in one run, so a second changes nothing. While the floor was judged
        # against the ceiling as it stood, this took two runs to settle.
        path = self._write(tmp_path, "dep-a", "maf-sandbox>=0.6.0,<0.7")
        assert ranges.run("0.8.0", tmp_path) == [path]
        assert "maf-sandbox>=0.8.0,<0.9" in path.read_text("utf-8")
        assert ranges.run("0.8.0", tmp_path) == []

    def test_a_patch_release_moves_nothing(self, tmp_path: Path):
        self._write(tmp_path, "dep-a", "maf-sandbox>=0.8.0,<0.9")
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
            "[project]\nname = 'dep-a'\ndependencies = ['maf-sandbox>=0.7.0,<0.8']\n",
            "utf-8",
        )
        assert ranges.run("0.8.0", tmp_path) == [path]
        assert "maf-sandbox>=0.8.0,<0.9" in path.read_text("utf-8")

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
            'dependencies = ["maf-sandbox-acas>=0.1.0,<0.3", "maf-sandbox>=0.7.0,<0.8"]\n',
            "utf-8",
        )

        assert ranges.run("0.8.0", tmp_path) == [path]

        text = path.read_text("utf-8")
        assert "maf-sandbox>=0.8.0,<0.9" in text
        assert "maf-sandbox-acas>=0.1.0,<0.3" in text  # the sibling, exactly as it was
