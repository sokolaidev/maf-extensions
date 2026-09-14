"""The version a TestPyPI rehearsal names must be unused on its upload destination.

Equality is where it can be wrong silently. An index carrying `0.38.0` refuses an upload named
`0.38`, because PEP 440 reads the absent component as zero — a comparison on the strings passes
and the collision surfaces at the upload, after the whole gate has run.

The index itself is the only part not exercised here.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_rehearsal_version.py"
sys.path.insert(0, str(_ROOT / "scripts"))  # the script imports its sibling index reader
_spec = importlib.util.spec_from_file_location("check_rehearsal_version", _SCRIPT)
assert _spec and _spec.loader
rehearsal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rehearsal)

import pypi_index  # noqa: E402

_MANIFEST = """[project]
name = "maf-sandbox-example"
version = "0.4.0"
description = "a package"
"""


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A package directory under a repository root the script is pointed at."""
    directory = tmp_path / "packages" / "example"
    directory.mkdir(parents=True)
    (directory / "pyproject.toml").write_text(_MANIFEST, encoding="utf-8", newline="")
    monkeypatch.setattr(rehearsal, "_ROOT", tmp_path)
    return "example"


def _published(monkeypatch: pytest.MonkeyPatch, versions: list[str] | None) -> list[str]:
    """Pin what the indexes answer, and record the distribution they were asked about."""
    asked: list[str] = []

    def fetch(distribution: str) -> list[str] | None:
        asked.append(distribution)
        return versions

    monkeypatch.setattr(rehearsal, "fetch_published_versions", fetch)
    return asked


class TestWhenTwoSpellingsAreOneVersion:
    """`0.38` and `0.38.0` are the same release, and an index will only hold one of them."""

    @pytest.mark.parametrize(
        ("one", "other"), [("0.38", "0.38.0"), ("0.38.0", "0.38.0.0"), ("1", "1.0.0")]
    )
    def test_trailing_zeros_do_not_make_a_new_version(self, one: str, other: str):
        assert rehearsal.identity(one) == rehearsal.identity(other)

    @pytest.mark.parametrize(
        ("one", "other"),
        [("0.38.0", "0.38.1"), ("0.38.0", "0.38.0rc1"), ("0.38.0", "0.38.0.post1")],
    )
    def test_anything_else_stays_distinct(self, one: str, other: str):
        assert rehearsal.identity(one) != rehearsal.identity(other)


class TestCheckingTheCandidateAgainstTheIndexes:
    """The upload destination must have room for the candidate version."""

    def test_a_published_version_is_refused(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, package: str
    ):
        _published(monkeypatch, ["0.38.0", "0.37.0"])
        assert rehearsal.check(package, "0.38.0") == 1
        assert (
            "::error::maf-sandbox-example 0.38.0 is already on the upload index"
            in capsys.readouterr().out
        )

    @pytest.mark.parametrize("target_versions", [None, ["0.5.0"], ["0.6.0"]])
    def test_only_the_upload_destination_can_reserve_a_version(
        self, monkeypatch: pytest.MonkeyPatch, package: str, target_versions: list[str] | None
    ):
        target = "https://test.pypi.org/simple/"
        monkeypatch.setenv("UV_INDEX", target)
        monkeypatch.setenv("UV_DEFAULT_INDEX", target)
        monkeypatch.setenv("UV_INDEX_STRATEGY", "unsafe-best-match")
        requested: list[str] = []

        def read_json(url: str, **kwargs):
            requested.append(url)
            if url.startswith(target):
                return None if target_versions is None else {"versions": target_versions}
            return {"versions": ["0.6.0"]}

        monkeypatch.setattr(pypi_index, "read_json", read_json)
        assert rehearsal.check(package, "0.6.0") == int(target_versions == ["0.6.0"])
        assert requested == [f"{target}maf-sandbox-example/"]

    def test_a_version_spelled_differently_is_refused_under_its_published_name(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, package: str
    ):
        _published(monkeypatch, ["0.38.0"])
        assert rehearsal.check(package, "0.38") == 1
        assert "maf-sandbox-example 0.38.0 is already" in capsys.readouterr().out

    def test_an_unpublished_version_passes_and_says_what_the_newest_is(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, package: str
    ):
        _published(monkeypatch, ["0.38.0", "0.37.0"])
        assert rehearsal.check(package, "0.39.0") == 0
        assert "newest published is 0.38.0" in capsys.readouterr().out

    def test_a_distribution_nothing_has_published_passes(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, package: str
    ):
        _published(monkeypatch, None)
        assert rehearsal.check(package, "0.1.0") == 0
        assert "nothing published yet" in capsys.readouterr().out

    def test_it_asks_about_the_distribution_the_manifest_names(
        self, monkeypatch: pytest.MonkeyPatch, package: str
    ):
        """The dispatch names a directory; an index knows the `[project] name`."""
        asked = _published(monkeypatch, [])
        assert rehearsal.check(package, "0.39.0") == 0
        assert asked == ["maf-sandbox-example"]

    def test_an_unreachable_index_fails_the_step(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, package: str
    ):
        """Passing because the index could not be asked is the one outcome worth nothing."""

        def unreachable(_distribution: str) -> list[str] | None:
            raise pypi_index.IndexUnreachable("the index did not answer")

        monkeypatch.setattr(rehearsal, "fetch_published_versions", unreachable)
        argv = ["check_rehearsal_version.py", package, "0.39.0"]
        assert pypi_index.run_check(rehearsal.main, argv) == 1
        assert "::error::the index did not answer" in capsys.readouterr().err


class TestASpellingTheStampWouldNotKeep:
    """`uv version` normalises what it is written, and the built filenames follow it."""

    @pytest.mark.parametrize("candidate", ["v0.39.0", "0.39.0-rc1", "0.39.0RC1", "", "latest"])
    def test_it_is_refused_without_asking_the_index(
        self, monkeypatch: pytest.MonkeyPatch, package: str, candidate: str
    ):
        asked = _published(monkeypatch, ["0.38.0"])
        assert rehearsal.check(package, candidate) == 1
        assert asked == []

    @pytest.mark.parametrize("candidate", ["0.39.0", "0.39.0rc1", "0.39.0.post1", "0.39.0.dev1"])
    def test_the_spellings_a_release_here_can_carry_are_accepted(
        self, monkeypatch: pytest.MonkeyPatch, package: str, candidate: str
    ):
        _published(monkeypatch, ["0.38.0"])
        assert rehearsal.check(package, candidate) == 0


def test_the_script_runs_from_a_shell(tmp_path: Path):
    """`python3 scripts/...` is how the workflow calls it, with no path set up for the import."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "maf-sandbox", "v1.2.3"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        # The refusal carries an em dash, and a Windows child writes it in the console's
        # encoding unless it is told otherwise.
        env=os.environ | {"PYTHONUTF8": "1"},
    )
    assert result.returncode == 1
    assert "is not a canonical PEP 440 version" in result.stdout
