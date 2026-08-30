"""The publish-time guard against leaving the index unresolvable as a set.

`scripts/check_published_dependents_admit.py` refuses a maf-sandbox release that the
already-published dependents exclude. Its parsing and its verdict are pure functions of
metadata, so both are tested here; only the PyPI fetch is not.

The case that matters most is the ordering one. PyPI normalises `maf-sandbox>=0.6.0,<0.7` to
`maf-sandbox<0.7,>=0.6.0`, so a parser written against the shape the tree uses matches nothing
in the shape the index returns — and a check that finds no ceiling passes. That failure is
silent and permanent, which is worse than the bug it is meant to catch.
"""

from __future__ import annotations

import email.message
import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # the script imports its sibling for one shared comparison
_spec = importlib.util.spec_from_file_location(
    "check_published_dependents_admit", _SCRIPTS / "check_published_dependents_admit.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

# The retry the fetches inherit; `_patch_urlopen` zeroes its pause so no test waits.
import pypi_index  # noqa: E402


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://pypi.org/", code, "err", email.message.Message(), io.BytesIO(b"")
    )


class _Response:
    """The slice of an HTTP response ``json.load`` reads: a ``read()`` returning JSON bytes."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, object],
) -> None:
    """Route a URL to a payload dict (success) or an error to raise.

    Keys are matched as substrings, longest first, so a per-version segment like
    ``bicep/0.2.0/json`` is not shadowed by the top-level ``bicep/json``. A list value is
    consumed one reply per call and its last entry repeats, which is how a retried failure is
    written.
    """

    def fake_urlopen(url: str | urllib.request.Request, timeout: int | None = None) -> _Response:
        target = url.full_url if isinstance(url, urllib.request.Request) else url
        for key in sorted(routes, key=len, reverse=True):
            if key in target:
                result = routes[key]
                if isinstance(result, list):
                    result = result.pop(0) if len(result) > 1 else result[0]
                if isinstance(result, BaseException):
                    raise result
                return _Response(result)
        raise AssertionError(f"unexpected url {target}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # The retry is pinned in tests/test_pypi_index.py; here it only has to cost no seconds.
    monkeypatch.setattr(pypi_index, "FIRST_PAUSE_SECONDS", 0.0)


class TestReadingTheCeilingOutOfPublishedMetadata:
    """Order-independent, name-exact, and unbothered by markers and extras."""

    def test_the_shape_pypi_actually_returns(self):
        assert check.ceiling_of(["maf-sandbox<0.7,>=0.6.0"]) == (0, 7)

    def test_the_shape_the_tree_writes(self):
        assert check.ceiling_of(["maf-sandbox>=0.6.0,<0.7"]) == (0, 7)

    def test_a_marker_does_not_hide_it(self):
        assert check.ceiling_of(['maf-sandbox<0.7,>=0.6.0; python_version >= "3.12"']) == (0, 7)

    def test_a_sibling_sharing_the_prefix_is_not_read_as_the_core(self):
        assert check.ceiling_of(["maf-sandbox-acas<0.7,>=0.6.0"]) is None

    def test_an_unbounded_requirement_has_no_ceiling(self):
        assert check.ceiling_of(["maf-sandbox>=0.6.0"]) is None

    def test_an_inclusive_bound_is_not_mistaken_for_an_exclusive_one(self):
        assert check.ceiling_of(["maf-sandbox<=0.7.0,>=0.6.0"]) is None

    def test_other_requirements_are_ignored(self):
        assert check.ceiling_of(["azure-identity<2,>=1.25.1", "maf-sandbox<0.8,>=0.6.0"]) == (0, 8)


class TestWhatItReports:
    """Name a dependent whose published ceiling excludes the version going out — and only it."""

    def test_a_stale_ceiling_is_named(self):
        published = {"maf-sandbox-acas": ["maf-sandbox<0.7,>=0.6.0"]}
        excluded = check.exclusions(published, (0, 7, 0))
        assert len(excluded) == 1
        assert "maf-sandbox-acas" in excluded[0] and "0.7.0" in excluded[0]

    def test_a_widened_ceiling_has_nothing_to_report(self):
        published = {"maf-sandbox-acas": ["maf-sandbox<0.8,>=0.6.0"]}
        assert check.exclusions(published, (0, 7, 0)) == []

    def test_a_patch_under_the_old_ceiling_has_nothing_to_report(self):
        published = {"maf-sandbox-acas": ["maf-sandbox<0.7,>=0.6.0"]}
        assert check.exclusions(published, (0, 6, 2)) == []

    def test_an_unpublished_dependent_is_skipped(self):
        assert check.exclusions({"maf-sandbox-new": None}, (0, 7, 0)) == []

    def test_every_one_is_named_not_just_the_first(self):
        published = {
            "maf-sandbox-acas": ["maf-sandbox<0.7,>=0.6.0"],
            "maf-sandbox-wslc": ["maf-sandbox<0.7,>=0.6.0"],
            "maf-sandbox-docker": ["maf-sandbox<0.8,>=0.6.0"],
        }
        excluded = check.exclusions(published, (0, 7, 0))
        assert len(excluded) == 2
        assert not any("docker" in line for line in excluded)


class TestItNoLongerRefuses:
    """The inversion #633 made to this check's pull-request-time twin, made here too.

    Refusing is what ordered every core release behind its dependents': a core the ceilings
    excluded could not go out until all five shipped again, whether or not anything was broken.
    A ceiling that excludes the candidate puts that dependent *out of reach* of it, which for a
    breaking release is the point. What refuses on evidence is `check_core_against_dependents`,
    which runs the admitting dependents' own suites (#628).
    """

    @staticmethod
    def _stale(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(check, "dependent_distributions", lambda _: ["maf-sandbox-acas"])
        monkeypatch.setattr(check, "fetch_requires_dist", lambda _: ["maf-sandbox<0.7,>=0.6.0"])

    def test_an_excluded_version_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        self._stale(monkeypatch)
        assert check.main(["prog", "0.7.0"]) == 0

    def test_what_it_prints_goes_to_stdout_not_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """A notice in the stream that reads as a fault is a refusal with extra steps."""
        self._stale(monkeypatch)
        check.main(["prog", "0.7.0"])
        captured = capsys.readouterr()
        assert "maf-sandbox-acas" in captured.out
        assert captured.err == ""

    def test_it_names_what_follows_rather_than_a_thing_to_do_first(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        self._stale(monkeypatch)
        check.main(["prog", "0.7.0"])
        printed = capsys.readouterr().out
        assert "the live samples" in printed
        assert "unsatisfiable set" in printed

    def test_a_version_every_ceiling_admits_still_says_so(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        monkeypatch.setattr(check, "dependent_distributions", lambda _: ["maf-sandbox-acas"])
        monkeypatch.setattr(check, "fetch_requires_dist", lambda _: ["maf-sandbox<0.9,>=0.6.0"])
        assert check.main(["prog", "0.7.0"]) == 0
        assert "every published dependent admits" in capsys.readouterr().out


class TestTheDependentsItLooksUp:
    """Derived from this repository, so a new package is covered without being listed."""

    def test_it_finds_the_dependents_and_not_the_core(self):
        found = check.dependent_distributions(Path(__file__).resolve().parent.parent)
        assert "maf-sandbox" not in found
        assert "maf-sandbox-acas" in found
        assert len(found) >= 5


class TestRequirementNames:
    @pytest.mark.parametrize(
        ("requirement", "expected"),
        [
            ("maf-sandbox<0.7,>=0.6.0", "maf-sandbox"),
            ("maf-sandbox", "maf-sandbox"),
            ("maf-sandbox[extra]>=0.6.0", "maf-sandbox"),
            ('azure-core[aio]>=1.0; extra == "x"', "azure-core"),
        ],
    )
    def test_the_name_is_read_without_its_constraint(self, requirement: str, expected: str):
        assert check._requirement_name(requirement) == expected


class TestFetchRequiresDist:
    """``fetch_requires_dist`` resolves the newest version from the simple index and reads its per-version document."""

    def test_reads_the_newest_version_from_the_simple_index(self, monkeypatch):
        # No top-level ``info`` route is registered: the simple index and per-version documents
        # are the whole of what the guard may consult, so an unexpected URL fails the test.
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13"], "yanked": False}
                },
            },
        )
        assert check.fetch_requires_dist("maf-sandbox-bicep") == ["maf-sandbox<0.13"]

    def test_a_never_released_distribution_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, {"simple/maf-sandbox-bicep/": _http_error(404)})
        assert check.fetch_requires_dist("maf-sandbox-bicep") is None

    def test_a_5xx_on_the_simple_index_that_outlasts_the_retries_is_fatal(self, monkeypatch):
        _patch_urlopen(monkeypatch, {"simple/maf-sandbox-bicep/": _http_error(500)})
        with pytest.raises(pypi_index.IndexUnreachable):
            check.fetch_requires_dist("maf-sandbox-bicep")

    def test_a_yanked_newest_falls_back_to_the_older_non_yanked(self, monkeypatch):
        # The newest yanked release is skipped: no unpinned resolution selects it, and an
        # installer settles on the next non-yanked release, whose ceiling is the one that must
        # admit the new version.
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13"], "yanked": True}
                },
                "maf-sandbox-bicep/0.2.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.12,>=0.10.0"], "yanked": False}
                },
            },
        )
        assert check.fetch_requires_dist("maf-sandbox-bicep") == ["maf-sandbox<0.12,>=0.10.0"]

    def test_a_5xx_on_the_per_version_fetch_that_outlasts_the_retries_is_fatal(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": _http_error(500),
            },
        )
        with pytest.raises(pypi_index.IndexUnreachable):
            check.fetch_requires_dist("maf-sandbox-bicep")
