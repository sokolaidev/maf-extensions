"""The publish-time guard against shipping a core the dependents can no longer import.

`scripts/check_published_dependents_work.py` is the other half of the admit check: the admit
check reads each dependent's ceiling and refuses if it excludes the version going out; this one
installs the candidate core wheel beside each dependent *version* that admits it and refuses if
that version no longer imports. Its at-risk filter and its verdict are pure functions of
metadata, so both are tested here; only the venv install and the import are not — the one impure
step is passed in as a fake. The subprocess sequence that step runs is pinned here by mocking
``subprocess.run``, so no venv is created and uv is never invoked. No network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(
    0, str(_SCRIPTS)
)  # the script imports its siblings for shared comparisons
_spec = importlib.util.spec_from_file_location(
    "check_published_dependents_work", _SCRIPTS / "check_published_dependents_work.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_ARGV0 = "scripts/check_published_dependents_work.py"


class TestImportModule:
    @pytest.mark.parametrize(
        ("distribution", "module"),
        [
            ("maf-sandbox", "maf_sandbox"),
            ("maf-sandbox-bicep", "maf_sandbox_bicep"),
            ("maf-sandbox-docker", "maf_sandbox_docker"),
        ],
    )
    def test_the_dash_becomes_an_underscore(self, distribution: str, module: str):
        assert check.import_module(distribution) == module


class TestAtRisk:
    """The inverse of the admit check, per published version: ceiling is None or admits."""

    def test_a_ceiling_that_admits_is_at_risk(self):
        # bicep <0.13 admitted 0.11.0 — the dependent version the admit check passes and this tests.
        published = {"maf-sandbox-bicep": [("0.5.6", ["maf-sandbox<0.13,>=0.10.0"])]}
        assert check.at_risk(published, (0, 11, 0)) == [("maf-sandbox-bicep", "0.5.6")]

    def test_a_ceiling_that_excludes_is_not_at_risk(self):
        # <0.3 correctly refused 0.11.0; that is the admit check's refusal, not this check's job.
        published = {"maf-sandbox-bicep": [("0.3.0", ["maf-sandbox<0.3,>=0.2.0"])]}
        assert check.at_risk(published, (0, 11, 0)) == []

    def test_an_unbounded_ceiling_is_at_risk(self):
        published = {"maf-sandbox-bicep": [("0.5.6", ["maf-sandbox>=0.10.0"])]}
        assert check.at_risk(published, (0, 11, 0)) == [("maf-sandbox-bicep", "0.5.6")]

    def test_an_unpublished_dependent_is_skipped(self):
        assert check.at_risk({"maf-sandbox-new": None}, (0, 11, 0)) == []

    def test_the_order_pypi_returns_is_irrelevant(self):
        # The admit check parses order-independently; this check inherits that, so it must too.
        published = {"maf-sandbox-bicep": [("0.5.6", ["maf-sandbox>=0.10.0,<0.13"])]}
        assert check.at_risk(published, (0, 11, 0)) == [("maf-sandbox-bicep", "0.5.6")]

    def test_an_old_loose_ceiling_version_is_at_risk_even_when_latest_moved_on(self):
        # #248 symptom 2: docker 0.2.0 (<0.12) admits 0.11.0 though docker's latest has ratcheted
        # past it — latest-only would never test 0.2.0. The whole point of the all-versions widening.
        published = {
            "maf-sandbox-docker": [
                ("0.2.0", ["maf-sandbox<0.12,>=0.10.0"]),  # admits 0.11.0
                ("0.6.0", ["maf-sandbox<0.13,>=0.11.0"]),  # admits 0.11.0 too
            ]
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            ("maf-sandbox-docker", "0.2.0"),
            ("maf-sandbox-docker", "0.6.0"),
        ]

    def test_versions_are_sorted_within_a_dependent(self):
        published = {
            "maf-sandbox-bicep": [
                ("0.10.0", ["maf-sandbox<0.13"]),
                ("0.2.0", ["maf-sandbox<0.13"]),
                ("0.9.0", ["maf-sandbox<0.13"]),
            ]
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            ("maf-sandbox-bicep", "0.2.0"),
            ("maf-sandbox-bicep", "0.9.0"),
            ("maf-sandbox-bicep", "0.10.0"),
        ]

    def test_dependents_are_sorted_by_name(self):
        published = {
            "maf-sandbox-wslc": [("0.5.0", ["maf-sandbox<0.13"])],
            "maf-sandbox-acas": [("0.5.0", ["maf-sandbox<0.13"])],
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            ("maf-sandbox-acas", "0.5.0"),
            ("maf-sandbox-wslc", "0.5.0"),
        ]

    def test_it_is_the_inverse_of_the_admit_check_refusals(self):
        published = {
            "maf-sandbox-acas": [
                ("0.5.0", ["maf-sandbox<0.7,>=0.6.0"])
            ],  # excludes 0.11.0
            "maf-sandbox-bicep": [
                ("0.5.6", ["maf-sandbox<0.13,>=0.10.0"])
            ],  # admits 0.11.0
            "maf-sandbox-docker": [("0.6.0", ["maf-sandbox>=0.6.0"])],  # unbounded
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            ("maf-sandbox-bicep", "0.5.6"),
            ("maf-sandbox-docker", "0.6.0"),
        ]


def _ok(_core_wheel: Path, _distribution: str, _version: str) -> str | None:
    return None


def _breaks_docker_020(
    _core_wheel: Path, distribution: str, version_str: str
) -> str | None:
    # The 0.11.0 incident's second red run: the OLD docker 0.2.0 imports a name the core removed.
    if distribution == "maf-sandbox-docker" and version_str == "0.2.0":
        return "ImportError: cannot import name 'CallerContext' from 'maf_sandbox'"
    return None


class TestBreaks:
    """The verdict over an injected install/import, so the decision is testable offline."""

    def test_every_import_clean_is_no_failure(self):
        candidates = [("maf-sandbox-bicep", "0.5.6"), ("maf-sandbox-docker", "0.6.0")]
        assert check.breaks(Path("core.whl"), candidates, _ok) == []

    def test_a_break_is_named_with_its_version_and_reason(self):
        candidates = [("maf-sandbox-docker", "0.2.0"), ("maf-sandbox-docker", "0.6.0")]
        failures = check.breaks(Path("core.whl"), candidates, _breaks_docker_020)
        assert failures == [
            "maf-sandbox-docker==0.2.0: ImportError: cannot import name 'CallerContext' "
            "from 'maf_sandbox'"
        ]

    def test_only_the_broken_version_is_reported(self):
        candidates = [("maf-sandbox-docker", "0.2.0"), ("maf-sandbox-docker", "0.6.0")]
        failures = check.breaks(Path("core.whl"), candidates, _breaks_docker_020)
        assert len(failures) == 1
        assert "0.6.0" not in failures[0]


class _CompletedProcess:
    """The slice of ``subprocess.CompletedProcess`` the script reads: returncode, stdout, stderr."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestInstallAndImport:
    """The subprocess sequence that builds the venv, installs, and imports.

    The verdict tests above pass a fake for the whole step; these pin the argv the real one builds
    and the branch each failure returns, by mocking ``subprocess.run``. No network and no real venv
    — uv is never invoked.
    """

    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        results: list[_CompletedProcess],
    ) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> _CompletedProcess:
            calls.append(cmd)
            return results[len(calls) - 1]

        monkeypatch.setattr(check.subprocess, "run", fake_run)
        return calls

    def test_the_success_sequence_pins_the_version_and_imports(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        calls = self._patch(
            monkeypatch,
            [_CompletedProcess(0), _CompletedProcess(0), _CompletedProcess(0)],
        )
        assert (
            check.install_and_import(
                tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6"
            )
            is None
        )
        assert len(calls) == 3
        assert calls[0][:2] == ["uv", "venv"]
        assert calls[1][:4] == ["uv", "pip", "install", "--python"]
        assert str(tmp_path / "core.whl") in calls[1]
        assert "maf-sandbox-bicep==0.5.6" in calls[1]
        assert calls[2][1] == "-c"
        assert "import maf_sandbox_bicep" in calls[2]

    def test_a_venv_creation_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        self._patch(monkeypatch, [_CompletedProcess(1, stderr="uv: boom")])
        error = check.install_and_import(
            tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6"
        )
        assert error == "uv venv failed: uv: boom"

    def test_an_install_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        self._patch(
            monkeypatch,
            [_CompletedProcess(0), _CompletedProcess(1, stderr="No solution found")],
        )
        error = check.install_and_import(
            tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6"
        )
        assert error == "install failed: No solution found"

    def test_an_import_failure_reports_the_last_traceback_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ImportError: cannot import name 'CallerContext'\n"
        )
        self._patch(
            monkeypatch,
            [
                _CompletedProcess(0),
                _CompletedProcess(0),
                _CompletedProcess(1, stderr=stderr),
            ],
        )
        error = check.install_and_import(
            tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6"
        )
        assert error == "ImportError: cannot import name 'CallerContext'"

    def test_an_import_failure_with_no_output_still_names_the_module(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        self._patch(
            monkeypatch,
            [_CompletedProcess(0), _CompletedProcess(0), _CompletedProcess(1)],
        )
        error = check.install_and_import(
            tmp_path / "core.whl", "maf-sandbox-bicep", "0.5.6"
        )
        assert error == "import maf_sandbox_bicep failed"


class TestMain:
    """The wiring — fetch, filter, verify, exit code — with both impure steps faked."""

    def _wheel(self, tmp_path: Path) -> Path:
        wheel = tmp_path / "maf_sandbox-0.11.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        return wheel

    def test_the_wrong_number_of_arguments(self, capsys: pytest.CaptureFixture[str]):
        assert check.main([_ARGV0]) == 2
        assert "usage:" in capsys.readouterr().err

    def test_too_many_arguments_with_the_flag_is_still_wrong(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        assert (
            check.main(
                [_ARGV0, "0.11.0", str(self._wheel(tmp_path)), "--latest-only", "extra"]
            )
            == 2
        )
        assert "usage:" in capsys.readouterr().err

    def test_a_missing_core_wheel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        assert check.main([_ARGV0, "0.11.0", str(tmp_path / "no-such.whl")]) == 1
        assert "no core wheel" in capsys.readouterr().err

    def test_all_versions_with_a_broken_old_version_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # docker 0.2.0 (<0.12) admits 0.11.0 and breaks — the #248 symptom-2 path, caught at build
        # time because build runs all-versions. The latest docker 0.6.0 admits too but imports clean.
        def fake_fetch(distribution: str) -> dict[str, list[str]]:
            if distribution == "maf-sandbox-docker":
                return {
                    "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
                    "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
                }
            return {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]}

        monkeypatch.setattr(check, "fetch_version_requirements", fake_fetch)
        monkeypatch.setattr(check, "install_and_import", _breaks_docker_020)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 1
        captured = capsys.readouterr()
        assert "maf-sandbox-docker==0.2.0: ImportError" in captured.err
        assert "Release order" in captured.err

    def test_all_versions_all_clean_names_every_admitting_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        def fake_fetch(distribution: str) -> dict[str, list[str]]:
            if distribution == "maf-sandbox-docker":
                return {
                    "0.2.0": ["maf-sandbox<0.12,>=0.10.0"],
                    "0.6.0": ["maf-sandbox<0.13,>=0.11.0"],
                }
            return {"0.6.0": ["maf-sandbox<0.13,>=0.10.0"]}

        monkeypatch.setattr(check, "fetch_version_requirements", fake_fetch)
        monkeypatch.setattr(check, "install_and_import", _ok)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "imports against it" in out
        assert "maf-sandbox-docker==0.2.0" in out

    def test_latest_only_tests_just_the_latest_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        monkeypatch.setattr(
            check, "fetch_latest", lambda _d: ("0.6.0", ["maf-sandbox<0.13,>=0.10.0"])
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        assert (
            check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path)), "--latest-only"])
            == 0
        )
        out = capsys.readouterr().out
        assert "maf-sandbox-bicep==0.6.0" in out
        assert "0.2.0" not in out

    def test_every_version_excluded_leaves_nothing_to_verify(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # every published version excludes the candidate — the admit check's refusal, not a pass
        monkeypatch.setattr(
            check,
            "fetch_version_requirements",
            lambda _d: {"0.5.0": ["maf-sandbox<0.11,>=0.10.0"]},
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 0
        assert "nothing to verify" in capsys.readouterr().out
