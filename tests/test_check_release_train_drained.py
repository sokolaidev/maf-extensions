"""The annotation that says a live red belongs to the order of the release train.

`scripts/check_release_train_drained.py` decides nothing — it prints a verdict a workflow turns
into a run-summary note. What it must get right is which question it asks: the *floor*, not the
ceiling. Every published dependent admitted `maf-sandbox` 0.18.0 while none had been rebuilt for
it, so a ceiling-shaped check would have called that train drained (#512).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # the script imports two siblings for the parsing it reuses
_spec = importlib.util.spec_from_file_location(
    "check_release_train_drained", _SCRIPTS / "check_release_train_drained.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

#: The 0.18.0 train at 18:11 on 2026-08-20: the core is out, none of the dependents is.
_MID_DRAIN = {
    "maf-sandbox-acas": ["maf-sandbox<0.19,>=0.17.0"],
    "maf-sandbox-bicep": ["maf-sandbox<0.19,>=0.17.0"],
}


class TestWhoIsBehind:
    """A dependent is behind when its floor predates the published core's minor."""

    def test_a_dependent_built_against_the_previous_core_is_named(self):
        assert check.behind(_MID_DRAIN, (0, 18, 0)) == [
            "maf-sandbox-acas on PyPI declares maf-sandbox>=0.17.0, "
            "which predates the published core 0.18.0",
            "maf-sandbox-bicep on PyPI declares maf-sandbox>=0.17.0, "
            "which predates the published core 0.18.0",
        ]

    def test_the_admitting_ceiling_does_not_make_it_caught_up(self):
        """`<0.19` admits 0.18.0. The whole point is that admitting is not the question."""
        assert check.behind({"maf-sandbox-acas": ["maf-sandbox<0.19,>=0.17.0"]}, (0, 18, 0))

    def test_a_dependent_built_against_this_core_is_not_named(self):
        assert check.behind({"maf-sandbox-acas": ["maf-sandbox<0.20,>=0.18.0"]}, (0, 18, 0)) == []

    def test_a_core_patch_strands_nobody(self):
        """A floor of `>=0.18.0` is caught up with 0.18.1: the comparison is at the minor."""
        assert check.behind({"maf-sandbox-acas": ["maf-sandbox<0.20,>=0.18.0"]}, (0, 18, 1)) == []

    def test_a_dependent_ahead_of_the_published_core_is_not_named(self):
        assert check.behind({"maf-sandbox-acas": ["maf-sandbox<0.21,>=0.19.0"]}, (0, 18, 0)) == []

    def test_an_unpublished_dependent_is_skipped(self):
        assert check.behind({"maf-sandbox-acas": None}, (0, 18, 0)) == []

    def test_a_dependent_declaring_no_floor_is_not_guessed_at(self):
        assert check.behind({"maf-sandbox-acas": ["maf-sandbox<0.20"]}, (0, 18, 0)) == []

    def test_every_lagging_dependent_is_named_not_just_the_first(self):
        assert len(check.behind(_MID_DRAIN, (0, 18, 0))) == 2


class TestTheVerdictLine:
    """The workflow reads the first line and nothing else, so it has to be exactly one of two."""

    @pytest.fixture
    def published(self, monkeypatch: pytest.MonkeyPatch):
        def _install(core: str, dependents: dict[str, list[str] | None]) -> None:
            monkeypatch.setattr(check, "fetch_published_versions", lambda _: [core])
            monkeypatch.setattr(check, "dependent_distributions", lambda _: sorted(dependents))
            monkeypatch.setattr(check, "fetch_requires_dist", lambda name: dependents[name])

        return _install

    def test_a_draining_train_says_so_on_the_first_line(self, capsys, published):
        published("0.18.0", _MID_DRAIN)
        assert check.main(["check_release_train_drained.py"]) == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed[0] == "train=draining"
        assert len(printed) == 3

    def test_a_drained_train_says_so_on_the_first_line(self, capsys, published):
        published("0.18.1", {"maf-sandbox-acas": ["maf-sandbox<0.20,>=0.18.0"]})
        assert check.main(["check_release_train_drained.py"]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "train=drained"

    def test_a_draining_train_still_exits_zero(self, capsys, published):
        """It annotates and never gates: a non-zero exit here would colour a shipped release."""
        published("0.18.0", _MID_DRAIN)
        assert check.main(["check_release_train_drained.py"]) == 0

    def test_an_unpublished_core_is_not_a_verdict(self, capsys, published, monkeypatch):
        monkeypatch.setattr(check, "fetch_published_versions", lambda _: None)
        assert check.main(["check_release_train_drained.py"]) == 2
        assert "train=" not in capsys.readouterr().out

    def test_an_argument_is_refused(self, capsys):
        assert check.main(["check_release_train_drained.py", "0.18.0"]) == 2
