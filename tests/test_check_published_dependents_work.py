"""The publish-time guard against shipping a core the dependents can no longer import.

`scripts/check_published_dependents_work.py` is the other half of the admit check: the admit
check reads each dependent's ceiling and refuses if it excludes the version going out; this one
installs the candidate core wheel beside each dependent that admits it and refuses if the
dependent no longer imports. Its at-risk filter and its verdict are pure functions of metadata,
so both are tested here; only the venv install and the import are not — the one impure step is
passed in as a fake.

The numbers in the fixtures are 0.11.0's. That release renamed ``WorkspaceContext`` to
``CallerContext`` in the core; bicep's ceiling ``<0.13`` admitted it, so a latest-only check run
at the time would have installed bicep against 0.11.0 and watched the import fail. The filter
keeps that dependent and drops one whose ``<0.11`` correctly excluded the breaking release.
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
    """The inverse of the admit check: ceiling is None or admits the released version."""

    def test_a_ceiling_that_admits_is_at_risk(self):
        # bicep <0.13 admitted 0.11.0 — the dependent the admit check passes and this one tests.
        published = {"maf-sandbox-bicep": ["maf-sandbox<0.13,>=0.10.0"]}
        assert check.at_risk(published, (0, 11, 0)) == ["maf-sandbox-bicep"]

    def test_a_ceiling_that_excludes_is_not_at_risk(self):
        # <0.11 correctly refused 0.11.0; that is the admit check's refusal, not this check's job.
        published = {"maf-sandbox-bicep": ["maf-sandbox<0.11,>=0.10.0"]}
        assert check.at_risk(published, (0, 11, 0)) == []

    def test_an_unbounded_ceiling_is_at_risk(self):
        published = {"maf-sandbox-bicep": ["maf-sandbox>=0.10.0"]}
        assert check.at_risk(published, (0, 11, 0)) == ["maf-sandbox-bicep"]

    def test_an_unpublished_dependent_is_skipped(self):
        assert check.at_risk({"maf-sandbox-new": None}, (0, 11, 0)) == []

    def test_the_order_pypi_returns_is_irrelevant(self):
        # The admit check parses order-independently; this check inherits that, so it must too.
        published = {"maf-sandbox-bicep": ["maf-sandbox>=0.10.0,<0.13"]}
        assert check.at_risk(published, (0, 11, 0)) == ["maf-sandbox-bicep"]

    def test_it_is_the_inverse_of_the_admit_check_refusals(self):
        published = {
            "maf-sandbox-acas": ["maf-sandbox<0.7,>=0.6.0"],  # excludes 0.11.0
            "maf-sandbox-bicep": ["maf-sandbox<0.13,>=0.10.0"],  # admits 0.11.0
            "maf-sandbox-docker": ["maf-sandbox>=0.6.0"],  # unbounded
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            "maf-sandbox-bicep",
            "maf-sandbox-docker",
        ]

    def test_dependents_are_sorted_by_name(self):
        published = {
            "maf-sandbox-wslc": ["maf-sandbox<0.13"],
            "maf-sandbox-acas": ["maf-sandbox<0.13"],
        }
        assert check.at_risk(published, (0, 11, 0)) == [
            "maf-sandbox-acas",
            "maf-sandbox-wslc",
        ]


def _ok(_core_wheel: Path, _distribution: str) -> str | None:
    return None


def _breaks_bicep(_core_wheel: Path, distribution: str) -> str | None:
    if distribution != "maf-sandbox-bicep":
        return None
    return "ImportError: cannot import name 'CallerContext' from 'maf_sandbox'"


class TestBreaks:
    """The verdict over an injected install/import, so the decision is testable offline."""

    def test_every_import_clean_is_no_failure(self):
        candidates = ["maf-sandbox-bicep", "maf-sandbox-docker"]
        assert check.breaks(Path("core.whl"), candidates, _ok) == []

    def test_a_break_is_named_with_its_reason(self):
        candidates = ["maf-sandbox-bicep", "maf-sandbox-docker"]
        failures = check.breaks(Path("core.whl"), candidates, _breaks_bicep)
        assert failures == [
            "maf-sandbox-bicep: ImportError: cannot import name 'CallerContext' "
            "from 'maf_sandbox'"
        ]

    def test_only_the_broken_dependent_is_reported(self):
        candidates = ["maf-sandbox-bicep", "maf-sandbox-docker"]
        failures = check.breaks(Path("core.whl"), candidates, _breaks_bicep)
        assert len(failures) == 1
        assert "docker" not in failures[0]


class TestMain:
    """The wiring — fetch, filter, verify, exit code — with both impure steps faked."""

    def _wheel(self, tmp_path: Path) -> Path:
        wheel = tmp_path / "maf_sandbox-0.11.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        return wheel

    def test_the_wrong_number_of_arguments(self, capsys: pytest.CaptureFixture[str]):
        assert check.main([_ARGV0]) == 2
        assert "usage:" in capsys.readouterr().err

    def test_a_missing_core_wheel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        assert check.main([_ARGV0, "0.11.0", str(tmp_path / "no-such.whl")]) == 1
        assert "no core wheel" in capsys.readouterr().err

    def test_every_admitting_dependent_importing_passes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # every real dependent admits 0.11.0 under this ceiling, and every import is clean
        monkeypatch.setattr(
            check, "fetch_requires_dist", lambda _d: ["maf-sandbox<0.13,>=0.10.0"]
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "imports against it" in out
        assert "maf-sandbox-bicep" in out

    def test_one_broken_dependent_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        monkeypatch.setattr(
            check, "fetch_requires_dist", lambda _d: ["maf-sandbox<0.13,>=0.10.0"]
        )
        monkeypatch.setattr(check, "install_and_import", _breaks_bicep)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 1
        captured = capsys.readouterr()
        assert "maf-sandbox-bicep: ImportError" in captured.err
        assert "Release order" in captured.err

    def test_every_dependent_excluded_leaves_nothing_to_verify(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # every published dependent excludes the candidate — the admit check's refusal, not a pass
        monkeypatch.setattr(
            check, "fetch_requires_dist", lambda _d: ["maf-sandbox<0.11,>=0.10.0"]
        )
        monkeypatch.setattr(check, "install_and_import", _ok)
        assert check.main([_ARGV0, "0.11.0", str(self._wheel(tmp_path))]) == 0
        assert "nothing to verify" in capsys.readouterr().out
