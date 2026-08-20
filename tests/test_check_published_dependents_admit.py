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
    """Route a URL to a payload dict (success) or an ``HTTPError`` (raise).

    Keys are matched as substrings, longest first, so a per-version segment like
    ``bicep/0.2.0/json`` is not shadowed by the top-level ``bicep/json``.
    """

    def fake_urlopen(url: str | urllib.request.Request, timeout: int | None = None) -> _Response:
        target = url.full_url if isinstance(url, urllib.request.Request) else url
        for key in sorted(routes, key=len, reverse=True):
            if key in target:
                result = routes[key]
                if isinstance(result, urllib.error.HTTPError):
                    raise result
                return _Response(result)
        raise AssertionError(f"unexpected url {target}")

    monkeypatch.setattr(check.urllib.request, "urlopen", fake_urlopen)


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


class TestTheVerdict:
    """Refuse only a dependent whose published ceiling excludes the version going out."""

    def test_a_stale_ceiling_is_refused(self):
        published = {"maf-sandbox-acas": ["maf-sandbox<0.7,>=0.6.0"]}
        problems = check.refusals(published, (0, 7, 0))
        assert len(problems) == 1
        assert "maf-sandbox-acas" in problems[0] and "0.7.0" in problems[0]

    def test_a_widened_ceiling_passes(self):
        published = {"maf-sandbox-acas": ["maf-sandbox<0.8,>=0.6.0"]}
        assert check.refusals(published, (0, 7, 0)) == []

    def test_a_patch_under_the_old_ceiling_passes(self):
        published = {"maf-sandbox-acas": ["maf-sandbox<0.7,>=0.6.0"]}
        assert check.refusals(published, (0, 6, 2)) == []

    def test_an_unpublished_dependent_is_skipped(self):
        assert check.refusals({"maf-sandbox-new": None}, (0, 7, 0)) == []

    def test_every_offender_is_named_not_just_the_first(self):
        published = {
            "maf-sandbox-acas": ["maf-sandbox<0.7,>=0.6.0"],
            "maf-sandbox-wslc": ["maf-sandbox<0.7,>=0.6.0"],
            "maf-sandbox-docker": ["maf-sandbox<0.8,>=0.6.0"],
        }
        problems = check.refusals(published, (0, 7, 0))
        assert len(problems) == 2
        assert not any("docker" in p for p in problems)


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

    def test_a_non_404_error_on_the_simple_index_is_fatal(self, monkeypatch):
        _patch_urlopen(monkeypatch, {"simple/maf-sandbox-bicep/": _http_error(500)})
        with pytest.raises(urllib.error.HTTPError):
            check.fetch_requires_dist("maf-sandbox-bicep")

    def test_a_yanked_newest_returns_none(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": {
                    "info": {"requires_dist": ["maf-sandbox<0.13"], "yanked": True}
                },
            },
        )
        assert check.fetch_requires_dist("maf-sandbox-bicep") is None

    def test_a_non_404_error_on_the_per_version_fetch_is_fatal(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            {
                "simple/maf-sandbox-bicep/": {"versions": ["0.2.0", "0.6.0"]},
                "maf-sandbox-bicep/0.6.0/json": _http_error(500),
            },
        )
        with pytest.raises(urllib.error.HTTPError):
            check.fetch_requires_dist("maf-sandbox-bicep")
