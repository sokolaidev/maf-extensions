"""The annotation that says a live red belongs to the order of the release train.

`scripts/check_release_train_drained.py` decides nothing — it prints a verdict a workflow turns
into a run-summary note. What it must get right is which question it asks: whether a dependent
has been *published* since the core, not what its dependency floor says.
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

#: The 0.18.0 train: the core went up at 18:09 and the dependents followed over the next hour.
_CORE_UP = "2026-08-20T18:09:58.786819Z"
_STALE = "2026-08-19T09:00:00.000000Z"
_MID_DRAIN = {"maf-sandbox-acas": _STALE, "maf-sandbox-bicep": _STALE}


class TestWhoIsBehind:
    """A dependent is behind when its newest upload predates the core's."""

    def test_a_dependent_that_has_not_gone_out_since_the_core_is_named(self):
        assert check.behind(_MID_DRAIN, _CORE_UP, (0, 18, 0)) == [
            f"maf-sandbox-acas on PyPI was last published at {_STALE}, before maf-sandbox 0.18.0 at {_CORE_UP}",
            f"maf-sandbox-bicep on PyPI was last published at {_STALE}, before maf-sandbox 0.18.0 at {_CORE_UP}",
        ]

    def test_a_dependent_published_after_the_core_is_not_named(self):
        assert check.behind({"a": "2026-08-20T18:52:33.445827Z"}, _CORE_UP, (0, 18, 0)) == []

    def test_a_floor_that_never_moves_is_not_mistaken_for_a_drain(self):
        """RELEASING.md raises a floor only when a dependent needs the version, so an old
        minimum can be permanent by design. Publication time is what separates the two."""
        assert check.behind({"a": "2026-08-20T18:52:33.445827Z"}, _CORE_UP, (0, 18, 0)) == []

    def test_a_dependent_published_in_the_same_second_is_not_behind(self):
        assert check.behind({"a": _CORE_UP}, _CORE_UP, (0, 18, 0)) == []

    def test_a_dependent_with_no_upload_time_is_not_guessed_at(self):
        assert check.behind({"a": None}, _CORE_UP, (0, 18, 0)) == []

    def test_every_lagging_dependent_is_named_not_just_the_first(self):
        assert len(check.behind(_MID_DRAIN, _CORE_UP, (0, 18, 0))) == 2


class TestTheVerdictLine:
    """The workflow reads the first line and nothing else, so it has to be exactly one of two."""

    @pytest.fixture
    def published(self, monkeypatch: pytest.MonkeyPatch):
        def _install(core: str, core_up: str | None, dependents: dict[str, str | None]) -> None:
            payloads: dict[str, dict | None] = {
                "maf-sandbox": {"versions": [core], "files": [{"upload-time": core_up}]}
            }
            for name, uploaded in dependents.items():
                payloads[name] = (
                    None if uploaded is None else {"files": [{"upload-time": uploaded}]}
                )
            monkeypatch.setattr(check, "fetch_simple", lambda name: payloads[name])
            monkeypatch.setattr(check, "dependent_distributions", lambda _: sorted(dependents))

        return _install

    def test_a_draining_train_says_so_on_the_first_line(self, capsys, published):
        published("0.18.0", _CORE_UP, _MID_DRAIN)
        assert check.main(["check_release_train_drained.py"]) == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed[0] == "train=draining"
        assert len(printed) == 3

    def test_a_drained_train_says_so_on_the_first_line(self, capsys, published):
        published("0.18.1", _CORE_UP, {"maf-sandbox-acas": "2026-08-20T19:01:27.943451Z"})
        assert check.main(["check_release_train_drained.py"]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "train=drained"

    def test_a_draining_train_still_exits_zero(self, capsys, published):
        """It annotates and never gates: a non-zero exit here would colour a shipped release."""
        published("0.18.0", _CORE_UP, _MID_DRAIN)
        assert check.main(["check_release_train_drained.py"]) == 0

    def test_an_unpublished_core_is_not_a_verdict(self, capsys, published, monkeypatch):
        monkeypatch.setattr(check, "fetch_simple", lambda _: None)
        assert check.main(["check_release_train_drained.py"]) == 2
        assert "train=" not in capsys.readouterr().out

    def test_a_core_with_no_upload_time_is_not_a_verdict(self, capsys, published):
        """PEP 700 made the field mandatory only for new uploads, so it can be absent."""
        published("0.18.0", None, _MID_DRAIN)
        assert check.main(["check_release_train_drained.py"]) == 2
        assert "train=" not in capsys.readouterr().out

    def test_an_argument_is_refused(self, capsys):
        assert check.main(["check_release_train_drained.py", "0.18.0"]) == 2
