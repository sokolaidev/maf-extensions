"""The wheel discovery and the drift report behind `scripts/check_suite_installs_together.py`.

`install` builds an environment and asks the resolver, so it is left to the live runs. What is
checked here is what the script decides around it: which wheels it treats as the set, and how it
tells "latest of everything" apart from "had to go back".
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # the script imports its siblings for the shared index helpers
_SPEC = importlib.util.spec_from_file_location(
    "check_suite_installs_together", _SCRIPTS / "check_suite_installs_together.py"
)
assert _SPEC and _SPEC.loader
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


def _wheel(directory: Path, name: str) -> Path:
    path = directory / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("placeholder", "")
    return path


class TestWhichWheelsFormTheSet:
    def test_the_core_is_part_of_the_set_here(self, tmp_path: Path):
        """Unlike the core gate, this one installs the family together — core included."""
        _wheel(tmp_path, "maf_sandbox-0.22.0-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl")
        assert sorted(check.wheels_in(tmp_path)) == ["maf-sandbox", "maf-sandbox-bicep"]

    def test_two_wheels_for_one_distribution_are_refused(self, tmp_path: Path):
        _wheel(tmp_path, "maf_sandbox_acas-0.12.0-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_acas-0.12.2-py3-none-any.whl")
        with pytest.raises(SystemExit, match="more than one wheel"):
            check.wheels_in(tmp_path)

    def test_something_else_in_the_directory_is_ignored(self, tmp_path: Path):
        _wheel(tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl")
        _wheel(tmp_path, "requests-2.34.2-py3-none-any.whl")
        assert list(check.wheels_in(tmp_path)) == ["maf-sandbox-bicep"]


class TestTheDriftReport:
    """A set that resolves is a pass; a set that had to go back is a pass worth reading."""

    def test_the_newest_of_everything_is_reported_without_comment(self):
        lines = check.report(
            {"maf-sandbox": "0.22.0", "maf-sandbox-bicep": "0.9.3"},
            {"maf-sandbox": "0.22.0", "maf-sandbox-bicep": "0.9.3"},
        )
        assert not any("not the newest" in line for line in lines)

    def test_a_version_the_resolver_went_back_for_is_marked(self):
        lines = check.report(
            {"maf-sandbox-codeact": "0.7.1"},
            {"maf-sandbox-codeact": "0.7.3"},
        )
        assert any("not the newest published" in line for line in lines)

    def test_a_distribution_with_no_published_version_is_not_marked(self):
        """A package that has never shipped cannot be behind anything."""
        lines = check.report({"maf-sandbox-new": "0.1.0"}, {})
        assert lines and not any("not the newest" in line for line in lines)

    def test_the_rows_are_sorted_so_two_runs_can_be_compared(self):
        lines = check.report({"maf-sandbox-wslc": "0.10.2", "maf-sandbox-acas": "0.12.2"}, {})
        assert "maf-sandbox-acas" in lines[0] and "maf-sandbox-wslc" in lines[1]


class TestTheCli:
    def test_an_empty_dist_directory_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        assert check.main(["prog", "--dist-dir", str(tmp_path)]) == 2
        assert "build them first" in capsys.readouterr().err


def _wheel_declaring(directory: Path, name: str, requirement: str) -> Path:
    """A wheel carrying one `Requires-Dist`, which is what `declared_range` reads."""
    path = directory / name
    distribution, release = name.split("-")[:2]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{distribution}-{release}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {distribution}\nRequires-Dist: {requirement}\n",
        )
    return path


class TestWhyItDoesNotCombine:
    """The table that replaced the resolver's own account of a core-range conflict.

    uv answers this by walking every historical version of every sibling — hundreds of wrapped
    lines whose decisive fact is two constraints. These read those two directly.
    """

    _CANDIDATE = "maf_sandbox_wslc-0.11.1-py3-none-any.whl"

    def test_the_core_entry_is_not_a_dependents(self):
        # `maf-sandbox-acas` starts with the core's own name, so a prefix match reports a bound
        # the core never declared — as the thing that decided the conflict.
        entries = ["maf-sandbox-acas<0.2", "maf-sandbox<0.23,>=0.21.0"]
        assert check._core_requirement(entries) == "maf-sandbox<0.23,>=0.21.0"

    def test_an_environment_marker_is_dropped(self):
        entry = 'maf-sandbox>=0.23.1,<0.25; python_version >= "3.12"'
        assert check._core_requirement([entry]) == "maf-sandbox>=0.23.1,<0.25"

    def test_a_dependent_only_list_names_no_core_requirement(self):
        assert check._core_requirement(["maf-sandbox-acas<0.2", "pytest"]) is None

    def test_a_sibling_whose_ceiling_excludes_the_floor_is_named(self, tmp_path: Path):
        wheel = _wheel_declaring(tmp_path, self._CANDIDATE, "maf-sandbox>=0.23.1,<0.25")
        published = {"maf-sandbox-acas": "maf-sandbox<0.23,>=0.21.0"}
        lines, explained = check.constraints("maf-sandbox-wslc", wheel, published)
        assert explained, "a ceiling below the candidate's floor is the whole explanation"
        assert "maf-sandbox>=0.23.1,<0.25" in lines[0]
        assert "excludes 0.23.1" in lines[1]

    def test_the_candidate_is_not_listed_against_itself(self, tmp_path: Path):
        wheel = _wheel_declaring(tmp_path, self._CANDIDATE, "maf-sandbox>=0.23.1,<0.25")
        published = {"maf-sandbox-wslc": "maf-sandbox<0.23,>=0.21.0"}
        lines, explained = check.constraints("maf-sandbox-wslc", wheel, published)
        assert len(lines) == 1, "the published self is an older version, not a sibling"
        assert not explained

    def test_overlapping_ranges_leave_it_unexplained(self, tmp_path: Path):
        """The honest half: agree on the core and the collision is somewhere else entirely.

        These packages also carry agent-framework and the Azure libraries. When the table
        explains nothing the caller prints the resolver's own account instead, so a `True` here
        would swallow the only account there is.
        """
        wheel = _wheel_declaring(tmp_path, self._CANDIDATE, "maf-sandbox>=0.23.1,<0.25")
        published = {"maf-sandbox-acas": "maf-sandbox>=0.22.0,<0.25"}
        lines, explained = check.constraints("maf-sandbox-wslc", wheel, published)
        assert not explained
        assert "excludes" not in lines[1]

    def test_a_sibling_with_no_ceiling_does_not_explain_anything(self, tmp_path: Path):
        wheel = _wheel_declaring(tmp_path, self._CANDIDATE, "maf-sandbox>=0.23.1,<0.25")
        published = {"maf-sandbox-acas": "maf-sandbox>=0.21.0"}
        _, explained = check.constraints("maf-sandbox-wslc", wheel, published)
        assert not explained
