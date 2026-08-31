"""The selection logic behind `scripts/check_dependent_works_with_published_cores.py`.

What is exercised here is everything the gate decides *before* it spawns anything: which range
the wheel declares, which published cores that admits, and which sibling wheels come along.
The install commands themselves are asserted here too — the sibling pass is where a dropped
argument would silently empty every throwaway environment — while the `uv` runs are left to
the live gate.
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


class TestTheCoreThisCheckoutBuilt:
    """`--local-core` — the fallback when nothing published satisfies the range.

    A change that uses a new core symbol from a dependent has to floor it on a release that
    does not exist yet, so the index can offer nothing to test against. The artifact this
    checkout built can, and the claim it settles — this code beside its own core — is the one
    a pull request is in a position to settle at all.
    """

    @staticmethod
    def _wheel_and_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirement: str):
        wheel = _wheel(tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl", [requirement])
        monkeypatch.setattr(check, "fetch_published_versions", lambda _: ["0.25.0"])
        monkeypatch.setattr(check, "fetch_requires_dist_for_version", lambda *_: [])
        core = tmp_path / "maf_sandbox-0.25.0-py3-none-any.whl"
        core.write_bytes(b"")
        return wheel, core

    def test_the_suite_runs_against_it_when_nothing_published_is_admitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        wheel, core = self._wheel_and_index(tmp_path, monkeypatch, "maf-sandbox>=0.26.0,<0.28")
        seen: list[object] = []
        monkeypatch.setattr(
            check, "run_suite", lambda w, c, t: (seen.append(c), (True, "12 passed"))[1]
        )
        code = check.main(["prog", "maf-sandbox-bicep", str(wheel), "--local-core", str(core)])
        assert code == 0
        assert seen == [core], "the wheel this checkout built, not a version to resolve"
        out = capsys.readouterr().out
        assert "maf_sandbox-0.25.0-py3-none-any.whl" in out
        assert "12 passed" in out

    def test_a_suite_that_fails_against_it_is_still_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """The fallback changes which core is tested, not whether the result counts."""
        wheel, core = self._wheel_and_index(tmp_path, monkeypatch, "maf-sandbox>=0.26.0,<0.28")
        monkeypatch.setattr(check, "run_suite", lambda *_: (False, "1 failed"))
        code = check.main(["prog", "maf-sandbox-bicep", str(wheel), "--local-core", str(core)])
        assert code == 1
        assert "FAIL" in capsys.readouterr().out

    def test_without_the_flag_the_same_range_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """What `publish-packages.yml` calls: an upload needs a published core."""
        wheel, _ = self._wheel_and_index(tmp_path, monkeypatch, "maf-sandbox>=0.26.0,<0.28")
        assert check.main(["prog", "maf-sandbox-bicep", str(wheel)]) == 1
        assert "uninstallable as declared" in capsys.readouterr().err

    def test_the_fallback_is_not_reached_while_a_published_core_is_admitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The flag adds a fallback, it does not replace the index."""
        wheel, core = self._wheel_and_index(tmp_path, monkeypatch, "maf-sandbox>=0.25.0,<0.28")
        seen: list[object] = []
        monkeypatch.setattr(check, "run_suite", lambda w, c, t: (seen.append(c), (True, "ok"))[1])
        assert check.main(["prog", "maf-sandbox-bicep", str(wheel), "--local-core", str(core)]) == 0
        assert seen == ["0.25.0"], "resolved from the index, not the local wheel"

    def test_a_relative_path_is_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The workflow passes `dist/...`, and the override needs a file URI, which a relative
        path cannot express — it reached CI as a `ValueError` rather than a verdict."""
        wheel, core = self._wheel_and_index(tmp_path, monkeypatch, "maf-sandbox>=0.26.0,<0.28")
        monkeypatch.chdir(tmp_path)
        seen: list[Path] = []
        monkeypatch.setattr(check, "run_suite", lambda w, c, t: (seen.append(c), (True, "ok"))[1])
        code = check.main(["prog", "maf-sandbox-bicep", str(wheel), "--local-core", core.name])
        assert code == 0
        assert seen[0].is_absolute() and seen[0].as_uri()

    def test_a_core_wheel_that_is_not_there_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = check.main(
            ["prog", "maf-sandbox-bicep", "w.whl", "--local-core", str(tmp_path / "absent.whl")]
        )
        assert code == 2
        assert "no core wheel at" in capsys.readouterr().err

    def test_a_missing_path_after_the_flag_is_a_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ):
        assert check.main(["prog", "maf-sandbox-bicep", "w.whl", "--local-core"]) == 2
        assert "--local-core <wheel>" in capsys.readouterr().err


class TestTheInstallCommands:
    """What `run_suite` asks `uv` for, read off the subprocess call.

    Each install succeeds whatever it installs, so the arguments are what decide the pairing:
    the override naming the forced core, the sibling pass on the local-core path, the sibling
    requirements on the published path. The index is faked to the one version each test needs,
    so the verdict does not move with what PyPI happens to hold.
    """

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch, published: list[str]) -> list[list[str]]:
        seen: list[list[str]] = []
        monkeypatch.setattr(
            check,
            "fetch_published_versions",
            lambda dist: published if dist == "maf-sandbox" else [],
        )
        monkeypatch.setattr(check, "fetch_requires_dist_for_version", lambda *_: [])
        monkeypatch.setattr(
            check.subprocess,
            "run",
            lambda argv, **_: (
                seen.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "", "")
            ),
        )
        return seen

    def test_dockers_suite_gets_wslc_when_tested_against_the_local_core(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The pairing the gate exists to prove includes the sibling a suite names outright:
        docker's parity check hard-asserts `maf_sandbox_wslc` imports, so an environment
        without it fails for the wrong reason — or, with the assert quietly dropped, passes
        without testing the parity at all. Here the core is the checkout's own artifact, the
        window the flag exists for, so the sibling rides the `--no-deps` pass."""
        wheel = _wheel(
            tmp_path, "maf_sandbox_docker-0.11.0-py3-none-any.whl", ["maf-sandbox>=0.28.0,<0.29"]
        )
        _wheel(tmp_path, "maf_sandbox_wslc-0.13.0-py3-none-any.whl", ["maf-sandbox>=0.28.0,<0.29"])
        core = _wheel(tmp_path, "maf_sandbox-0.27.0-py3-none-any.whl", [])
        seen = self._capture(monkeypatch, published=[])
        code = check.main(["prog", "maf-sandbox-docker", str(wheel), "--local-core", str(core)])
        assert code == 0
        installs = [a for a in seen if a[:2] == ["uv", "pip"]]
        no_deps = [a for a in installs if "--no-deps" in a]
        assert no_deps, "the siblings ride a --no-deps pass, or their floors reach the resolver"
        assert any("maf_sandbox_wslc-0.13.0-py3-none-any.whl" in " ".join(a) for a in no_deps), (
            "docker's suite asserts its wslc sibling is importable; the install must carry it"
        )
        assert not any("maf_sandbox_docker-0.11.0" in " ".join(a) for a in no_deps), (
            "the candidate rides the resolving pass, its siblings the forced one"
        )

    def test_the_published_core_path_pins_the_newest_sibling_admitting_each_core(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The published-core pairing installs what a consumer could assemble: the candidate,
        the published core, and per sibling the newest published version whose own metadata
        admits that core — not the newest release unconditionally, which would fail the
        candidate over an unrelated sibling's floor move. A sibling with no version admitting
        the core stays out rather than failing the environment."""
        wheel = _wheel(
            tmp_path, "maf_sandbox_codeact-0.8.0-py3-none-any.whl", ["maf-sandbox>=0.27.0,<0.29"]
        )
        seen = self._capture(monkeypatch, published=["0.27.0"])
        # docker 0.10.0 admits 0.27.0; a 0.11.0 floored on 0.28.0 does not and must not appear.
        published_requires = {
            ("maf-sandbox-docker", "0.11.0"): ["maf-sandbox>=0.28.0,<0.29"],
            ("maf-sandbox-docker", "0.10.0"): ["maf-sandbox>=0.27.0,<0.29"],
            ("maf-sandbox-wslc", "0.13.0"): ["maf-sandbox>=0.28.0,<0.29"],
        }
        monkeypatch.setattr(
            check,
            "fetch_requires_dist_for_version",
            lambda dist, ver: published_requires.get((dist, ver), []),
        )
        monkeypatch.setattr(
            check,
            "fetch_published_versions",
            lambda dist: (
                ["0.27.0"]
                if dist == "maf-sandbox"
                else (["0.11.0", "0.10.0"] if dist == "maf-sandbox-docker" else ["0.13.0"])
            ),
        )
        code = check.main(["prog", "maf-sandbox-codeact", str(wheel)])
        assert code == 0
        installs = [a for a in seen if a[:2] == ["uv", "pip"]]
        assert len(installs) == 1, "one resolving install, not a candidate pass plus a forced one"
        assert "--no-deps" not in installs[0]
        joined = " ".join(installs[0])
        assert "maf-sandbox==0.27.0" in joined
        assert "maf-sandbox-docker==0.10.0" in joined, (
            "the newest sibling admitting the core, not the newest release"
        )
        assert "maf-sandbox-wslc" not in joined, (
            "a sibling with no version admitting the core stays out"
        )
        assert "maf-sandbox-acas==0.13.0" in joined and "maf-sandbox-bicep==0.13.0" in joined, (
            "siblings defaulting to an empty metadata admit the core at their newest"
        )

    def test_the_core_is_pinned_by_the_override_and_answered_by_an_operand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An override rewrites a requirement and never adds one, so the core is also named as
        an operand — and the wheel that satisfies it is the forced artifact, not the index."""
        wheel = _wheel(
            tmp_path, "maf_sandbox_bicep-0.9.3-py3-none-any.whl", ["maf-sandbox>=0.26.0,<0.28"]
        )
        core = _wheel(tmp_path, "maf_sandbox-0.27.0-py3-none-any.whl", [])
        seen = self._capture(monkeypatch, published=[])  # nothing published admits the range
        check.main(["prog", "maf-sandbox-bicep", str(wheel), "--local-core", str(core)])
        first = next(
            a
            for a in seen
            if a[:2] == ["uv", "pip"] and "--no-deps" not in a and "--overrides" in a
        )
        assert "maf_sandbox-0.27.0-py3-none-any.whl" in " ".join(first), (
            "the operand is the artifact, so the pin cannot resolve down to the index"
        )

    def test_a_missing_path_after_the_flag_is_a_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ):
        assert check.main(["prog", "maf-sandbox-bicep", "w.whl", "--local-core"]) == 2
        assert "--local-core <wheel>" in capsys.readouterr().err
