"""The selection logic behind `scripts/check_dependent_works_with_published_cores.py`.

What is exercised here is everything the gate decides *before* it spawns anything: which range
the wheel declares, which published cores that admits, and which sibling wheels come along.
`run_suite` builds an environment and runs pytest in it, so it is left to the live runs.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # the script imports its siblings for the shared index helpers
_SCRIPT = _SCRIPTS / "check_dependent_works_with_published_cores.py"
_spec = importlib.util.spec_from_file_location("check_dependent_works", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _wheel(directory: Path, name: str, requires: list[str] | None = None) -> Path:
    """A wheel carrying just enough METADATA for the range to be read off it."""
    path = directory / name
    distribution = name.split("-", 1)[0]
    lines = ["Metadata-Version: 2.1", f"Name: {distribution.replace('_', '-')}", "Version: 0.0.0"]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires or []]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{distribution}-0.0.0.dist-info/METADATA", "\n".join(lines))
    return path


class TestTheRangeIsReadOffTheWheel:
    def test_both_ends_are_returned(self, tmp_path: Path):
        wheel = _wheel(
            tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl", ["maf-sandbox>=0.22.0,<0.24"]
        )
        assert check.declared_range(wheel) == ((0, 22, 0), (0, 24))

    def test_whitespace_the_metadata_may_carry_is_tolerated(self, tmp_path: Path):
        wheel = _wheel(
            tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl", ["maf-sandbox >= 0.22.0 , < 0.24"]
        )
        assert check.declared_range(wheel) == ((0, 22, 0), (0, 24))

    def test_another_requirement_naming_the_core_does_not_answer(self, tmp_path: Path):
        """`maf-sandbox-docker` contains `maf-sandbox`, and must not be read as it."""
        wheel = _wheel(
            tmp_path,
            "maf_sandbox_codeact-0.7.3-py3-none-any.whl",
            ["maf-sandbox-docker>=0.7.0,<0.9", "maf-sandbox>=0.22.0,<0.24"],
        )
        assert check.declared_range(wheel) == ((0, 22, 0), (0, 24))

    def test_a_wheel_declaring_no_range_is_refused(self, tmp_path: Path):
        wheel = _wheel(
            tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl", ["agent-framework-core>=1"]
        )
        with pytest.raises(SystemExit, match="declares no"):
            check.declared_range(wheel)

    def test_a_file_without_metadata_is_refused(self, tmp_path: Path):
        path = tmp_path / "not-a-wheel.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("nothing.txt", "")
        with pytest.raises(SystemExit, match="carries no METADATA"):
            check.declared_range(path)


class TestWhichPublishedCoresAreAdmitted:
    @staticmethod
    def _index(monkeypatch: pytest.MonkeyPatch, versions: list[str], yanked: tuple[str, ...] = ()):
        monkeypatch.setattr(check, "fetch_published_versions", lambda _: list(versions))
        monkeypatch.setattr(
            check,
            "fetch_requires_dist_for_version",
            lambda _, candidate: None if candidate in yanked else [],
        )

    def test_only_versions_inside_both_ends_are_returned(self, monkeypatch: pytest.MonkeyPatch):
        self._index(monkeypatch, ["0.21.0", "0.22.0", "0.23.0", "0.24.0"])
        assert check.admitted_published_cores((0, 22, 0), (0, 24)) == ["0.22.0", "0.23.0"]

    def test_the_floor_is_inclusive_and_the_ceiling_is_not(self, monkeypatch: pytest.MonkeyPatch):
        self._index(monkeypatch, ["0.22.0", "0.24.0"])
        assert check.admitted_published_cores((0, 22, 0), (0, 24)) == ["0.22.0"]

    def test_the_result_is_oldest_first(self, monkeypatch: pytest.MonkeyPatch):
        """The floor is where a mis-declared range shows itself, so it is reported first."""
        self._index(monkeypatch, ["0.23.0", "0.22.0", "0.22.1"])
        assert check.admitted_published_cores((0, 22, 0), (0, 24)) == ["0.22.0", "0.22.1", "0.23.0"]

    def test_a_yanked_version_is_not_tested_against(self, monkeypatch: pytest.MonkeyPatch):
        """Unpinned resolution never selects one, so a break there is not a real-user break."""
        self._index(monkeypatch, ["0.22.0", "0.23.0"], yanked=("0.23.0",))
        assert check.admitted_published_cores((0, 22, 0), (0, 24)) == ["0.22.0"]

    def test_an_empty_result_is_returned_rather_than_raised(self, monkeypatch: pytest.MonkeyPatch):
        """`main` turns this into the 'uninstallable as declared' refusal, with the range named."""
        self._index(monkeypatch, ["0.20.0"])
        assert check.admitted_published_cores((0, 22, 0), (0, 24)) == []

    def test_a_core_that_was_never_published_is_fatal(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(check, "fetch_published_versions", lambda _: None)
        with pytest.raises(SystemExit, match="never been published"):
            check.admitted_published_cores((0, 22, 0), (0, 24))


class TestTheSiblingsThatComeAlong:
    def test_the_other_dependents_are_found_and_the_subject_is_not(self, tmp_path: Path):
        subject = _wheel(tmp_path, "maf_sandbox_docker-0.7.3-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_wslc-0.10.2-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl")
        assert [path.name for path in check.sibling_wheels(subject)] == [
            "maf_sandbox_bicep-0.9.3-py3-none-any.whl",
            "maf_sandbox_wslc-0.10.2-py3-none-any.whl",
        ]

    def test_the_core_wheel_is_not_a_sibling(self, tmp_path: Path):
        """It is installed from the index at a pinned version — a local copy would shadow it."""
        subject = _wheel(tmp_path, "maf_sandbox_docker-0.7.3-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox-0.22.0-py3-none-any.whl")
        assert check.sibling_wheels(subject) == []

    def test_two_wheels_for_one_distribution_are_refused(self, tmp_path: Path):
        """A stale artifact would otherwise decide silently which version was tested."""
        subject = _wheel(tmp_path, "maf_sandbox_docker-0.7.3-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_acas-0.12.0-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_acas-0.12.2-py3-none-any.whl")
        with pytest.raises(SystemExit, match="more than one wheel"):
            check.sibling_wheels(subject)


class TestTheCli:
    def test_the_core_is_refused_as_a_usage_error(self, capsys: pytest.CaptureFixture[str]):
        """Not 0: a check that never ran must not report as a check that passed."""
        assert check.main(["prog", "maf-sandbox", "unused.whl"]) == 2
        assert "this check is for its dependents" in capsys.readouterr().err

    def test_a_package_with_no_test_tree_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]):
        assert check.main(["prog", "maf-sandbox-nonexistent", "unused.whl"]) == 2
        assert "nothing to run" in capsys.readouterr().err

    def test_wrong_argument_count_is_a_usage_error(self):
        assert check.main(["prog"]) == 2

    def test_a_range_no_published_core_satisfies_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        wheel = _wheel(
            tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl", ["maf-sandbox>=9.0.0,<9.1"]
        )
        monkeypatch.setattr(check, "fetch_published_versions", lambda _: ["0.22.0"])
        monkeypatch.setattr(check, "fetch_requires_dist_for_version", lambda *_: [])
        assert check.main(["prog", "maf-sandbox-bicep", str(wheel)]) == 1
        assert "uninstallable as declared" in capsys.readouterr().err


_CORE_PATH = "packages/maf-sandbox/src/maf_sandbox/_protocol.py"
_BACKEND_PATH = "packages/maf-sandbox-bicep/src/maf_sandbox_bicep/_kind.py"


class TestTheReleaseThisBranchWouldCut:
    """`pending_core_release` — the branch reading behind `--unreleased-core`.

    A change that adds to the core and uses it from a dependent has to raise that dependent's
    floor to a version nothing has published, so the range admits nothing until the same merge
    uploads the core. This is what tells that apart from a floor that is simply wrong.
    """

    @staticmethod
    def _at(monkeypatch: pytest.MonkeyPatch, current: str) -> None:
        monkeypatch.setattr(check, "core_version", lambda _: check.version(current))

    def test_the_release_a_breaking_core_change_would_cut_is_admitted(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`bump-minor-pre-major`, so below 1.0.0 a `feat!` is a minor like any other feature."""
        self._at(monkeypatch, "0.25.0")
        pending = check.pending_core_release(
            "feat!: a scope purge reports what it could not delete",
            [_CORE_PATH, _BACKEND_PATH],
            (0, 26, 0),
            (0, 28),
        )
        assert pending == "0.26.0"

    def test_a_change_that_does_not_touch_the_core_cuts_no_core_release(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Otherwise a dependent-only pull request could raise its floor past every artifact and
        be waved through on a release nothing would make."""
        self._at(monkeypatch, "0.25.0")
        assert (
            check.pending_core_release(
                "feat!: only the backend", [_BACKEND_PATH], (0, 26, 0), (0, 28)
            )
            is None
        )

    def test_a_title_that_releases_nothing_justifies_nothing(self, monkeypatch: pytest.MonkeyPatch):
        self._at(monkeypatch, "0.25.0")
        assert (
            check.pending_core_release("chore: tidy up", [_CORE_PATH], (0, 26, 0), (0, 28)) is None
        )

    def test_a_floor_above_the_release_is_still_uninstallable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The point of the flag is a floor the *next* release reaches, not any floor at all."""
        self._at(monkeypatch, "0.25.0")
        assert (
            check.pending_core_release("feat!: a scope purge", [_CORE_PATH], (0, 27, 0), (0, 28))
            is None
        )

    def test_a_release_above_the_ceiling_is_refused_too(self, monkeypatch: pytest.MonkeyPatch):
        """A range whose own ceiling excludes the release it is waiting for keeps nothing."""
        self._at(monkeypatch, "0.25.0")
        assert (
            check.pending_core_release("feat!: a scope purge", [_CORE_PATH], (0, 26, 0), (0, 26))
            is None
        )

    def test_a_patch_release_counts_when_the_floor_is_the_patch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Not only minors: a `fix:` that the dependent's floor waits on is the same shape."""
        self._at(monkeypatch, "0.25.0")
        assert (
            check.pending_core_release("fix: a corrected code", [_CORE_PATH], (0, 25, 1), (0, 27))
            == "0.25.1"
        )


class TestTheCliReadsTheBranchOnlyWhenAsked:
    @staticmethod
    def _wheel_and_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirement: str) -> Path:
        wheel = _wheel(tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl", [requirement])
        monkeypatch.setattr(check, "fetch_published_versions", lambda _: ["0.25.0"])
        monkeypatch.setattr(check, "fetch_requires_dist_for_version", lambda *_: [])
        monkeypatch.setattr(check, "core_version", lambda _: (0, 25, 0))
        return wheel

    def test_the_flag_accepts_a_range_only_this_branch_will_satisfy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        wheel = self._wheel_and_index(tmp_path, monkeypatch, "maf-sandbox>=0.26.0,<0.28")
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{_CORE_PATH}\n{_BACKEND_PATH}\n"))
        code = check.main(
            [
                "prog",
                "maf-sandbox-bicep",
                str(wheel),
                "--unreleased-core",
                "feat!: a scope purge reports what it could not delete",
            ]
        )
        assert code == 0
        assert "would release maf-sandbox 0.26.0" in capsys.readouterr().out

    def test_without_the_flag_the_same_range_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """What `publish-packages.yml` calls: by then the core is up, so a range nothing
        satisfies is the uninstallable wheel it has always been."""
        wheel = self._wheel_and_index(tmp_path, monkeypatch, "maf-sandbox>=0.26.0,<0.28")
        assert check.main(["prog", "maf-sandbox-bicep", str(wheel)]) == 1
        assert "uninstallable as declared" in capsys.readouterr().err

    def test_the_flag_does_not_rescue_a_floor_the_release_will_not_reach(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        wheel = self._wheel_and_index(tmp_path, monkeypatch, "maf-sandbox>=9.0.0,<9.1")
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{_CORE_PATH}\n"))
        code = check.main(
            ["prog", "maf-sandbox-bicep", str(wheel), "--unreleased-core", "feat!: a change"]
        )
        assert code == 1
        assert "uninstallable as declared" in capsys.readouterr().err

    def test_a_missing_title_after_the_flag_is_a_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ):
        assert check.main(["prog", "maf-sandbox-bicep", "w.whl", "--unreleased-core"]) == 2
        assert "--unreleased-core <title>" in capsys.readouterr().err
