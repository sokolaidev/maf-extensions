"""What `scripts/check_live_host_tools_dispatch_sample.py` will and will not let through.

`_HEALTHY` is a real run of `samples/15_acas_codeact_host_tools` against a live ACAS sandbox
and a live model, verbatim apart from the model's two replies.

The suite is built around what the check is *allowed* to fail a release for. Both routes run
Python in the sandbox, so both are held to what the interpreter printed and to who carried the
prices — properties of machinery. Nothing keys on a model's prose, and the tests below say so
by asserting that plausible model behaviour does **not** turn the run red.

Two of those tests exist because earlier versions of this check got it wrong on a live run.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_live_host_tools_dispatch_sample.py"
)
_spec = importlib.util.spec_from_file_location("check_dispatch", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
sys.modules["check_dispatch"] = check
_spec.loader.exec_module(check)


_PRICES = "['12.40', '3.05', '41.75']"

#: A real run. The model's two replies are the only edited lines.
_HEALTHY = f"""== 1. What the host wired ==

  registered:            unit_price
  identities the spec carries: ['app']
  Reading the aggregate sealed the registry: a later register() is refused, because
  this is the moment the surface became a policy the router can match.

== 2. The lookups happen inside the sandbox ==

218.15

  [measured] dispatches: 3 across 3 SKU(s)
  [measured] dispatch route: 3 lookup(s), 3 message(s), 12.35s, 1328 tokens
  [measured] dispatch route: the program printed 218.15: True
  [measured] dispatch route: prices the model wrote into code: none
  [measured] dispatch route: prices the model received: {_PRICES}
  [measured] round trip: 2 gap(s), min 1.12s, median 1.19s, max 1.25s

== 3. The lookups happen in the model's tool loop ==

218.15

  [measured] direct route: 3 lookup(s), 5 message(s), 4.09s, 1665 tokens
  [measured] direct route: the program printed 218.15: True
  [measured] direct route: prices the model wrote into code: {_PRICES}
  [measured] direct route: prices the model received: {_PRICES}

== 4. What the round trips bought ==

  [measured] prices the model handled, dispatched: 0 of 3
  [measured] prices the model handled, direct:     3 of 3

  [measured] Disposed 1 sandbox(es).
"""


def _without(line: str) -> str:
    """`_HEALTHY` with every line containing `line` removed."""
    kept = [row for row in _HEALTHY.splitlines() if line not in row]
    assert len(kept) < len(_HEALTHY.splitlines()), f"nothing to remove for {line!r}"
    return "\n".join(kept)


def _swap(old: str, new: str) -> str:
    assert old in _HEALTHY, f"{old!r} is not in the fixture"
    return _HEALTHY.replace(old, new)


class TestAHealthyRun:
    def test_it_passes(self):
        assert check.assess(_HEALTHY) == []


class TestBothInterpretersHadToGetItRight:
    """Both routes run Python, so neither gets a pass on the total."""

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_program_that_did_not_print_the_total_fails(self, route: str):
        broken = _swap(
            f"[measured] {route}: the program printed 218.15: True",
            f"[measured] {route}: the program printed 218.15: False",
        )
        assert any("did not print the exact total" in r for r in check.assess(broken))

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_missing_program_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: the program printed")) != []


class TestWhoCarriedThePrices:
    """The finding. Both halves are structural, so both are enforced."""

    def test_the_dispatched_route_writing_a_price_fails(self):
        """It cannot happen: the program is written before a dispatch can answer."""
        broken = _swap(
            "[measured] dispatch route: prices the model wrote into code: none",
            f"[measured] dispatch route: prices the model wrote into code: {_PRICES}",
        ).replace(
            "[measured] prices the model handled, dispatched: 0 of 3",
            "[measured] prices the model handled, dispatched: 3 of 3",
        )
        assert any("written before any dispatch happens" in r for r in check.assess(broken))

    def test_the_direct_route_writing_no_price_fails(self):
        """Then the contrast did not happen and there is nothing to compare."""
        broken = _swap(
            f"[measured] direct route: prices the model wrote into code: {_PRICES}",
            "[measured] direct route: prices the model wrote into code: none",
        ).replace(
            "[measured] prices the model handled, direct:     3 of 3",
            "[measured] prices the model handled, direct:     0 of 3",
        )
        assert any("contrast this sample exists to show" in r for r in check.assess(broken))

    def test_act_four_has_to_agree_with_the_routes(self):
        assert any(
            "disagree" in r
            for r in check.assess(
                _swap(
                    "[measured] prices the model handled, dispatched: 0 of 3",
                    "[measured] prices the model handled, dispatched: 2 of 3",
                )
            )
        )

    def test_a_missing_restatement_fails(self):
        assert any(
            "did not restate" in r
            for r in check.assess(_without("prices the model handled, direct"))
        )

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_missing_written_line_fails(self, route: str):
        assert check.assess(_without(f"{route}: prices the model wrote into code")) != []


class TestWhatTheModelSaidIsNeverRead:
    """A run must not go red for prose, and these are the shapes that would tempt it."""

    def test_a_program_that_printed_the_prices_is_not_a_failure(self):
        """Seen live. What the guest chooses to print is not what the transport decided."""
        assert check.assess(_HEALTHY) == []
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: prices the model received: " + _PRICES,
                    "[measured] dispatch route: prices the model received: none",
                )
            )
            == []
        )

    def test_a_wildly_slow_or_expensive_run_is_not_a_failure(self):
        """A slow control plane is a finding, not a failing grade."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 3 lookup(s), 3 message(s), 12.35s, 1328 tokens",
                    "[measured] dispatch route: 3 lookup(s), 9 message(s), 240.00s, 99999 tokens",
                )
            )
            == []
        )

    def test_an_untagged_line_is_not_read(self):
        forged = _HEALTHY.replace(
            "== 4. What the round trips bought ==",
            "direct route: prices the model wrote into code: none\n"
            "== 4. What the round trips bought ==",
        )
        assert check.assess(forged) == []

    def test_a_quoted_tagged_line_is_not_read_as_the_left_margin_one(self):
        """`quoted()` prefixes a model's tagged line with `> `, which the anchor rejects."""
        forged = _without("dispatch route: prices the model wrote into code").replace(
            "218.15\n",
            "218.15\n> [measured] dispatch route: prices the model wrote into code: none\n",
            1,
        )
        assert any("prices written" in r for r in check.assess(forged))

    def test_a_second_copy_of_a_line_is_refused_rather_than_resolved(self):
        doubled = _HEALTHY.replace(
            "  [measured] Disposed 1 sandbox(es).",
            "  [measured] Disposed 4 sandbox(es).\n  [measured] Disposed 1 sandbox(es).",
        )
        assert any("none of them can be trusted" in r for r in check.assess(doubled))


class TestEverySkuHadToBeAskedFor:
    def test_two_of_three_skus_fails(self):
        assert any(
            "a price it did not ask for" in r
            for r in check.assess(
                _swap(
                    "[measured] dispatches: 3 across 3 SKU(s)",
                    "[measured] dispatches: 2 across 2 SKU(s)",
                )
            )
        )

    def test_dispatching_nothing_fails(self):
        assert (
            check.assess(
                _swap(
                    "[measured] dispatches: 3 across 3 SKU(s)",
                    "[measured] dispatches: 0 across 0 SKU(s)",
                )
            )
            != []
        )

    def test_more_dispatches_than_skus_is_fine(self):
        """A model may write a program that asks twice; that is its business."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatches: 3 across 3 SKU(s)",
                    "[measured] dispatches: 6 across 3 SKU(s)",
                )
            )
            == []
        )

    @pytest.mark.parametrize("route", ["dispatch route", "direct route"])
    def test_a_route_that_never_reached_the_function_fails(self, route: str):
        line = [r for r in _HEALTHY.splitlines() if f"{route}: " in r and "lookup(s)" in r][0]
        assert any(
            "never reached the function" in r
            for r in check.assess(_swap(line, line.replace("3 lookup(s)", "0 lookup(s)")))
        )


class TestTheRoundTrip:
    def test_an_unordered_summary_fails(self):
        assert any(
            "not ordered" in r
            for r in check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.12s, median 1.19s, max 1.25s",
                    "[measured] round trip: 2 gap(s), min 1.50s, median 1.19s, max 1.25s",
                )
            )
        )

    def test_no_gaps_fails(self):
        assert any(
            "nothing was measured" in r
            for r in check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.12s, median 1.19s, max 1.25s",
                    "[measured] round trip: 0 gap(s), min 0.00s, median 0.00s, max 0.00s",
                )
            )
        )

    def test_a_fast_round_trip_is_not_rejected(self):
        """Below the poll interval means calls overlapped, which the transport allows."""
        assert (
            check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.12s, median 1.19s, max 1.25s",
                    "[measured] round trip: 5 gap(s), min 0.00s, median 0.04s, max 1.13s",
                )
            )
            == []
        )

    def test_a_missing_round_trip_line_fails(self):
        assert any("round trip" in r for r in check.assess(_without("round trip:")))


class TestTheBillableSandboxWentAway:
    def test_a_leaked_sandbox_fails(self):
        assert any(
            "bills until" in r
            for r in check.assess(
                _swap("[measured] Disposed 1 sandbox(es).", "[measured] Disposed 0 sandbox(es).")
            )
        )

    def test_a_missing_footer_fails(self):
        assert any("Disposed" in r for r in check.assess(_without("Disposed")))


class TestTheCommandLine:
    def test_a_healthy_file_exits_zero(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_HEALTHY, encoding="utf-8")
        assert check.main(["check", str(path)]) == 0
        assert "none of them on the other" in capsys.readouterr().out

    def test_a_broken_run_exits_one_and_says_why(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_without("Disposed"), encoding="utf-8")
        assert check.main(["check", str(path)]) == 1
        assert "FAIL" in capsys.readouterr().err

    def test_too_many_arguments_is_a_usage_error(self, capsys):
        assert check.main(["check", "a", "b"]) == 2
        assert "usage" in capsys.readouterr().err

    def test_stdin_is_read_when_no_path_is_given(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(_HEALTHY))
        assert check.main(["check"]) == 0


@pytest.mark.parametrize(
    "line",
    [
        "[measured] dispatches:",
        "dispatch route: the program printed",
        "direct route: the program printed",
        "dispatch route: prices the model wrote into code",
        "direct route: prices the model wrote into code",
        "prices the model handled, dispatched",
        "[measured] dispatch route: 3 lookup(s)",
        "[measured] direct route: 3 lookup(s)",
        "round trip:",
        "Disposed",
    ],
)
def test_every_measured_line_is_load_bearing(line: str):
    """Removing any one of them fails the check, so none is decoration."""
    assert check.assess(_without(line)) != []
