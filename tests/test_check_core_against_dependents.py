"""The selection and reporting behind `scripts/check_core_against_dependents.py`.

Everything the gate decides before it builds an environment: which wheels it found, whether a
version's tests can be recovered from its tag, how a verdict reads, and the install command
`run_suite` assembles. Building the environment and running pytest in it is left to the live
runs; which core that environment ends up holding is decided here, in the arguments.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

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


class _Recorder:
    """Stands in for `subprocess.run`, so the install command can be read without a venv.

    Every call succeeds. `run_suite` runs three — `uv venv`, `uv pip install`, then pytest —
    and only the second is the subject, so the other two just have to not stop it.
    """

    def __init__(self) -> None:
        self.install: list[str] = []
        self.overrides = ""

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["uv", "pip", "install"]:
            self.install = list(command)
            if "--overrides" in command:
                # Read now, not in the assertion: the file lives in `run_suite`'s temporary
                # directory and is gone by the time it returns.
                path = Path(command[command.index("--overrides") + 1])
                self.overrides = path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "1 passed", "")


def _install_command(
    monkeypatch: pytest.MonkeyPatch, requirements: list[str], core: Path
) -> _Recorder:
    """Run `run_suite` against a fake `subprocess` and hand back what it tried to install."""
    recorder = _Recorder()
    monkeypatch.setattr(check, "subprocess", SimpleNamespace(run=recorder))
    passed, _ = check.run_suite(requirements, core, Path("tests"))
    assert passed, "every faked call succeeds, so the suite must read as a pass"
    return recorder


class TestWhichCoreTheEnvironmentGets:
    """The core is forced, so no wheel's declared range can decide which one is installed.

    A `maf-sandbox` range says which cores a package is *published* against. Resolved rather
    than forced, an in-tree ceiling still a cycle behind the release refuses to build the
    environment at all, and the gate reports that in the shape of a failing suite — over a
    version RELEASING.md permits a core to be released at.
    """

    def test_the_core_is_forced_rather_than_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        core = _wheel(tmp_path, "maf_sandbox-0.33.0-py3-none-any.whl")
        recorder = _install_command(monkeypatch, ["maf-sandbox-bicep==0.13.0"], core)
        assert "--overrides" in recorder.install
        assert recorder.overrides == f"maf-sandbox @ {core.as_uri()}\n"

    def test_the_core_is_asked_for_as_well_as_forced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An override rewrites a requirement and never adds one — unasked-for, it is absent."""
        core = _wheel(tmp_path, "maf_sandbox-0.33.0-py3-none-any.whl")
        recorder = _install_command(monkeypatch, ["maf-sandbox-bicep==0.13.0"], core)
        assert str(core) in recorder.install

    def test_the_dependent_and_its_siblings_are_still_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        core = _wheel(tmp_path, "maf_sandbox-0.33.0-py3-none-any.whl")
        sibling = _wheel(tmp_path, "maf_sandbox_docker-0.15.0-py3-none-any.whl")
        recorder = _install_command(monkeypatch, ["dependent.whl", str(sibling)], core)
        assert "dependent.whl" in recorder.install
        assert str(sibling) in recorder.install
        assert "pytest" in recorder.install


class TestBothHalvesForceIt:
    """The published half needs it too: its siblings are branch wheels carrying branch bounds."""

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], Path]]:
        seen: list[tuple[list[str], Path]] = []

        def _fake(requirements: list[str], core: Path, tests: Path) -> tuple[bool, str]:
            seen.append((requirements, core))
            return True, "1 passed"

        monkeypatch.setattr(check, "run_suite", _fake)
        return seen

    def test_the_branch_half_hands_the_core_over_to_be_forced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        core = _wheel(tmp_path, "maf_sandbox-0.33.0-py3-none-any.whl")
        wheel = _wheel(tmp_path, "maf_sandbox_bicep-0.13.0-py3-none-any.whl")
        seen = self._capture(monkeypatch)

        check.assess_branch(core, {"maf-sandbox-bicep": wheel})

        (requirements, passed_core) = seen[0]
        assert passed_core == core
        assert str(core) not in requirements, "as a requirement it would be resolved, not forced"

    def test_the_published_half_hands_the_core_over_to_be_forced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        core = _wheel(tmp_path, "maf_sandbox-0.33.0-py3-none-any.whl")
        wheel = _wheel(tmp_path, "maf_sandbox_bicep-0.13.0-py3-none-any.whl")
        monkeypatch.setattr(check, "dependent_distributions", lambda root: ["maf-sandbox-bicep"])
        monkeypatch.setattr(check, "fetch_version_requirements", lambda name: {"0.12.0": []})
        monkeypatch.setattr(
            check, "at_risk", lambda published, released: [("maf-sandbox-bicep", "0.12.0")]
        )
        monkeypatch.setattr(check, "recover_tests", lambda tag, name, into: tmp_path / "tests")
        seen = self._capture(monkeypatch)

        check.assess_published((0, 33, 0), core, {"maf-sandbox-bicep": wheel}, tmp_path)

        (requirements, passed_core) = seen[0]
        assert passed_core == core
        assert str(core) not in requirements


class TestTheCli:
    def test_an_empty_dist_directory_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = check.main(
            ["prog", "0.22.0", str(tmp_path / "core.whl"), "--dist-dir", str(tmp_path)]
        )
        assert code == 2
        assert "build them first" in capsys.readouterr().err
