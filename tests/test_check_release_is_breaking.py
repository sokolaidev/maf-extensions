"""The breaking-release detector, read against the changelogs release-please actually wrote.

`scripts/check_release_is_breaking.py` answers one question: did this release declare breaking
changes? Below 1.0.0 the version number cannot, so the answer comes from the changelog section
— and the sections sit directly against each other, one heading apart. Most of what can go
wrong here is a slice that runs past its own section and reports its neighbour's heading, so
the real files are the test data rather than fixtures written to match the parser.

The other half is the refusal. A missing changelog or an absent version must exit non-zero, not
answer `breaking=false`: the caller acts on `true`, so a wrong `false` is the silent failure.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_release_is_breaking", _ROOT / "scripts" / "check_release_is_breaking.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_ARGV0 = "scripts/check_release_is_breaking.py"
_VERSION_HEADING = re.compile(r"^## \[(\d+(?:\.\d+)*)\]", re.MULTILINE)
#: The heading as release-please writes it, spelled out here rather than imported.
_RELEASE_PLEASE_HEADING = "### ⚠ BREAKING CHANGES"


def _changelogs() -> list[Path]:
    return sorted((_ROOT / "packages").glob("*/CHANGELOG.md"))


class TestTheReleasesThisRepositoryHasActuallyMade:
    """Real sections, including the two that sit either side of a boundary."""

    @pytest.mark.parametrize(
        ("package", "release", "expected"),
        [
            # The core's rename release, and the one directly above it in the file.
            ("maf-sandbox", "0.11.0", "breaking=true"),
            ("maf-sandbox", "0.12.0", "breaking=false"),
            # A dependent, at the top of its file — the slice starts against the preamble.
            ("maf-sandbox-codeact", "0.3.0", "breaking=true"),
            # Two breaking releases in a row, and the non-breaking one above them.
            ("maf-sandbox-acas", "0.5.0", "breaking=true"),
            ("maf-sandbox-acas", "0.4.0", "breaking=true"),
            ("maf-sandbox-acas", "0.6.0", "breaking=false"),
            # The hand-written first release: `## [0.1.0] - date`, with no compare link.
            ("maf-sandbox", "0.1.0", "breaking=false"),
        ],
    )
    def test_the_answer_matches_the_file(
        self,
        package: str,
        release: str,
        expected: str,
        capsys: pytest.CaptureFixture[str],
    ):
        assert check.main([_ARGV0, package, release]) == 0
        assert capsys.readouterr().out.strip() == expected

    def test_every_heading_in_every_changelog_lands_in_exactly_one_section(self):
        """No section claims a heading that is not its own, and none loses the one that is.

        The count on the left is a literal string search, deliberately not the module's own
        pattern: comparing the pattern against itself would hold just as well if it stopped
        recognising the heading release-please writes.
        """
        total_headings = 0
        total_breaking = 0
        for path in _changelogs():
            text = path.read_text("utf-8")
            headings = text.count(_RELEASE_PLEASE_HEADING)
            breaking = sum(
                check.is_breaking(check.section(text, release) or "")
                for release in _VERSION_HEADING.findall(text)
            )
            assert breaking == headings, f"{path.name}: {breaking} sections, {headings} headings"
            total_headings += headings
            total_breaking += breaking
        assert total_breaking == total_headings > 0


class TestWhenItCannotAnswer:
    """Loud, and never `breaking=false`."""

    def test_a_version_that_is_not_in_the_changelog(self, capsys: pytest.CaptureFixture[str]):
        assert check.main([_ARGV0, "maf-sandbox", "99.0.0"]) == 1
        captured = capsys.readouterr()
        assert "no section for 99.0.0" in captured.err
        assert "breaking=" not in captured.out

    def test_a_package_with_no_changelog(self, capsys: pytest.CaptureFixture[str]):
        assert check.main([_ARGV0, "maf-sandbox-nonesuch", "1.0.0"]) == 1
        captured = capsys.readouterr()
        assert "no changelog at" in captured.err
        assert "breaking=" not in captured.out

    @pytest.mark.parametrize("argv", [[_ARGV0], [_ARGV0, "maf-sandbox"], [_ARGV0, "a", "b", "c"]])
    def test_the_wrong_number_of_arguments(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ):
        assert check.main(argv) == 2
        assert "usage:" in capsys.readouterr().err


_TWO_SECTIONS = """# Changelog

## [0.3.0](https://example.invalid/compare/v0.2.0...v0.3.0) (2026-01-02)


### Features

* something ([#1](https://example.invalid/issues/1))

## [0.2.0](https://example.invalid/compare/v0.1.0...v0.2.0) (2026-01-01)


### ⚠ BREAKING CHANGES

* the old name is gone
"""


class TestFindingTheSection:
    def test_it_stops_at_the_next_release(self):
        assert "BREAKING" not in (check.section(_TWO_SECTIONS, "0.3.0") or "")

    def test_it_runs_to_the_end_of_the_file_for_the_last_release(self):
        assert "the old name is gone" in (check.section(_TWO_SECTIONS, "0.2.0") or "")

    def test_a_release_that_is_not_there_is_none(self):
        assert check.section(_TWO_SECTIONS, "0.4.0") is None

    def test_a_shorter_version_does_not_match_a_longer_one(self):
        assert check.section("## [0.11.0] - 2026-01-01\n", "0.1.0") is None


class TestReadingTheHeading:
    """Strict on the level and the words, indifferent to what decorates them."""

    @pytest.mark.parametrize(
        "heading",
        [
            "### ⚠ BREAKING CHANGES\n\n* gone\n",
            "### BREAKING CHANGES\n",
            "### 💥 BREAKING CHANGES\n",
            "###  ⚠  BREAKING CHANGES  \n",
        ],
    )
    def test_release_pleases_heading_however_it_is_decorated(self, heading: str):
        assert check.is_breaking(heading)

    @pytest.mark.parametrize(
        "not_a_heading",
        [
            # Words in front of the phrase turn a heading into prose about one.
            "### Not BREAKING CHANGES\n",
            "### A note on the BREAKING CHANGES entry above\n",
            # A deeper heading is something nested inside a section, not the section's own.
            "#### ⚠ BREAKING CHANGES\n",
            # The singular is the commit footer's wording, never the heading's.
            "### BREAKING CHANGE\n",
            "* restores the BREAKING CHANGES section (#1)\n",
            "### Features\n\n* something\n",
        ],
    )
    def test_what_is_not_that_heading(self, not_a_heading: str):
        assert not check.is_breaking(not_a_heading)


class TestTheChangelogItLooksFor:
    def test_it_is_the_one_in_the_package_directory(self):
        assert check.changelog_path(_ROOT, "maf-sandbox").is_file()

    def test_a_package_directory_that_does_not_exist_yields_a_path_that_does_not_either(
        self,
    ):
        assert not check.changelog_path(_ROOT, "maf-sandbox-nonesuch").exists()


def test_the_script_runs_from_a_shell(tmp_path: Path):
    """`python3 scripts/...` is how a workflow will call it, from wherever it happens to be."""
    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "check_release_is_breaking.py"),
            "maf-sandbox",
            "0.11.0",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "breaking=true"
