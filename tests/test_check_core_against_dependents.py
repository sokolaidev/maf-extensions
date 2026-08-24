"""The selection and reporting behind `scripts/check_core_against_dependents.py`.

Everything the gate decides before it builds an environment: which wheels it found, whether a
version's tests can be recovered from its tag, and how a verdict reads. `run_suite` creates a
venv and runs pytest in it, so it is left to the live runs.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # the script imports its siblings for the shared index helpers
_SPEC = importlib.util.spec_from_file_location(
    "check_core_against_dependents", _SCRIPTS / "check_core_against_dependents.py"
)
assert _SPEC and _SPEC.loader
check = importlib.util.module_from_spec(_SPEC)
# Registered before execution because the script defines a dataclass, and `dataclasses` resolves
# a field's type through `sys.modules[cls.__module__]` — absent, that lookup returns None.
sys.modules[_SPEC.name] = check
_SPEC.loader.exec_module(check)


def _wheel(directory: Path, name: str) -> Path:
    """An empty wheel — these tests only ever read its filename."""
    path = directory / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("placeholder", "")
    return path


class TestWhichWheelsItFound:
    def test_the_dependents_are_named_by_distribution(self, tmp_path: Path):
        _wheel(tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_docker-0.7.3-py3-none-any.whl")
        assert sorted(check.dependent_wheels(tmp_path)) == [
            "maf-sandbox-bicep",
            "maf-sandbox-docker",
        ]

    def test_the_core_wheel_is_not_one_of_them(self, tmp_path: Path):
        """It is the subject, passed in by path — finding it here would install it twice."""
        _wheel(tmp_path, "maf_sandbox-0.22.0-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl")
        assert list(check.dependent_wheels(tmp_path)) == ["maf-sandbox-bicep"]

    def test_two_wheels_for_one_distribution_are_refused(self, tmp_path: Path):
        _wheel(tmp_path, "maf_sandbox_acas-0.12.0-py3-none-any.whl")
        _wheel(tmp_path, "maf_sandbox_acas-0.12.2-py3-none-any.whl")
        with pytest.raises(SystemExit, match="more than one wheel"):
            check.dependent_wheels(tmp_path)

    def test_an_empty_directory_finds_nothing(self, tmp_path: Path):
        assert check.dependent_wheels(tmp_path) == {}


def _repository_with_a_tag(root: Path, tag: str, distribution: str) -> Path:
    """A throwaway repository carrying one tagged commit with ``distribution``'s test tree.

    Built here rather than read out of this repository, because `recover_tests` starts with
    `git tag --list` and a checkout without tags answers nothing. `tests.yml` fetches full
    history and `publish-packages.yml`'s build job does not, so a test that read this
    repository's own tags passed on every pull request and failed on every publish.
    """
    tests = root / "packages" / distribution / "tests"
    tests.mkdir(parents=True)
    (tests / "test_something.py").write_text("def test_it():\n    assert True\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
        ["git", "tag", tag],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return tests


class TestRecoveringAVersionsTests:
    """No sdist ships tests, so a published version's suite exists only at its release tag."""

    _TAG = "maf-sandbox-bicep-v0.9.3"
    _DIST = "maf-sandbox-bicep"

    def test_a_tag_yields_its_test_tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        origin = tmp_path / "origin"
        origin.mkdir()
        _repository_with_a_tag(origin, self._TAG, self._DIST)
        monkeypatch.setattr(check, "_ROOT", origin)

        recovered = check.recover_tests(self._TAG, self._DIST, tmp_path / "into")
        assert recovered is not None, "the tagged commit carries the test tree"
        assert (recovered / "test_something.py").is_file()

    def test_a_tag_that_does_not_exist_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Reported as a failure by the caller, never skipped — an untestable version is news."""
        origin = tmp_path / "origin"
        origin.mkdir()
        _repository_with_a_tag(origin, self._TAG, self._DIST)
        monkeypatch.setattr(check, "_ROOT", origin)

        assert check.recover_tests("maf-sandbox-bicep-v99.0.0", self._DIST, tmp_path / "x") is None

    def test_a_tag_without_that_package_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        origin = tmp_path / "origin"
        origin.mkdir()
        _repository_with_a_tag(origin, self._TAG, self._DIST)
        monkeypatch.setattr(check, "_ROOT", origin)

        assert check.recover_tests(self._TAG, "maf-sandbox-nope", tmp_path / "y") is None

    def test_a_checkout_without_tags_answers_nothing_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The shape that broke every publish: a shallow checkout carries no tags.

        The gate's own job fetches full history for this reason; a caller that does not gets
        `None` and reports an untestable version, rather than an exception mid-release.
        """
        origin = tmp_path / "origin"
        origin.mkdir()
        _repository_with_a_tag(origin, self._TAG, self._DIST)
        subprocess.run(["git", "tag", "-d", self._TAG], cwd=origin, check=True, capture_output=True)
        monkeypatch.setattr(check, "_ROOT", origin)

        assert check.recover_tests(self._TAG, self._DIST, tmp_path / "z") is None


class TestHowAVerdictReads:
    def test_the_outcome_comes_first_so_a_failure_is_findable(self):
        failed = check.Result("published", "maf-sandbox-bicep", "0.9.3", False, "1 failed")
        assert failed.line().startswith("FAIL published maf-sandbox-bicep 0.9.3")

    def test_a_pass_names_what_it_ran_against(self):
        passed = check.Result("branch", "maf-sandbox-bicep", "this checkout", True, "84 passed")
        assert "branch" in passed.line() and "this checkout" in passed.line()
        assert passed.line().startswith("ok")


class TestTheCli:
    def test_an_empty_dist_directory_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = check.main(
            ["prog", "0.22.0", str(tmp_path / "core.whl"), "--dist-dir", str(tmp_path)]
        )
        assert code == 2
        assert "build them first" in capsys.readouterr().err
