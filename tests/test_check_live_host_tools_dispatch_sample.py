"""What `scripts/check_live_host_tools_dispatch_sample.py` will and will not let through.

`_HEALTHY` is a real run of `samples/15_acas_codeact_host_tools` against a live ACAS sandbox
and a live model, kept verbatim apart from the model's own two replies — those are prose and
vary, and standing in for them is the point of the tag the check keys on.

The suite is organised around the one asymmetry that makes this check unusual: the dispatch
route is *required* to be exact and the direct route is *recorded*. Both halves are tested,
because an implementation that enforced the second would pass every happy-path test and go red
the first time a model got the addition right.
"""

from __future__ import annotations

import importlib.util
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


#: A real run. The two model replies are the only edited lines.
_HEALTHY = """== 1. What the host wired ==

  registered:            unit_price
  identities the spec carries: ['app']
  Reading the aggregate sealed the registry: a later register() is refused, because
  this is the moment the surface became a policy the router can match.

== 2. A program that cannot answer without calling out ==

218.15

  [measured] dispatches: 3 across 3 SKU(s)
  [measured] dispatch route: 3 call(s), 19.60s, 1762 tokens
  [measured] dispatch route: reply carries 218.15: True
  [measured] round trip: 2 gap(s), min 1.09s, median 1.09s, max 1.10s

== 3. The same question, answered without a sandbox ==

The total cost is **$233.20**.

  [measured] direct route: 3 call(s), 3.81s, 441 tokens
  [measured] direct route: reply carries 218.15: False

== 4. What the round trips bought ==

  The order costs 218.15. Both routes were given the same prices.

  [measured] dispatch route reached the exact total: True
  [measured] direct route reached the exact total:   False

  The dispatch route is slower and costs more tokens.

  [measured] Disposed 1 sandbox(es).
"""


def _without(line: str) -> str:
    """`_HEALTHY` with the one line containing `line` removed."""
    kept = [row for row in _HEALTHY.splitlines() if line not in row]
    assert len(kept) < len(_HEALTHY.splitlines()), f"nothing to remove for {line!r}"
    return "\n".join(kept)


def _swap(old: str, new: str) -> str:
    assert old in _HEALTHY, f"{old!r} is not in the fixture"
    return _HEALTHY.replace(old, new)


class TestAHealthyRun:
    def test_it_passes(self):
        assert check.assess(_HEALTHY) == []

    def test_the_direct_route_is_reported_as_missing_the_total(self):
        assert check.carried_direct(_HEALTHY) is False


class TestTheDispatchRouteMustBeExact:
    """The one hard assertion. A program computed this sum from prices the host supplied."""

    def test_an_inexact_dispatched_total_fails(self):
        broken = _swap(
            "[measured] dispatch route: reply carries 218.15: True",
            "[measured] dispatch route: reply carries 218.15: False",
        ).replace(
            "[measured] dispatch route reached the exact total: True",
            "[measured] dispatch route reached the exact total: False",
        )
        assert any("does not carry the exact total" in reason for reason in check.assess(broken))

    def test_a_missing_dispatch_verdict_fails(self):
        assert any(
            "dispatch route" in reason
            for reason in check.assess(_without("dispatch route: reply carries"))
        )


class TestTheDirectRouteIsRecordedNotRequired:
    """The half deliberately left unenforced, tested in both directions.

    A check that quietly required the model to be wrong would pass every other test here.
    """

    def test_a_wrong_direct_answer_is_not_a_failure(self):
        assert check.assess(_HEALTHY) == []

    def test_a_right_direct_answer_is_not_a_failure_either(self):
        lucky = _swap(
            "[measured] direct route: reply carries 218.15: False",
            "[measured] direct route: reply carries 218.15: True",
        ).replace(
            "[measured] direct route reached the exact total:   False",
            "[measured] direct route reached the exact total:   True",
        )
        assert check.assess(lucky) == []
        assert check.carried_direct(lucky) is True

    def test_the_direct_route_still_has_to_be_reported(self):
        assert check.assess(_without("direct route: reply carries")) != []

    def test_a_direct_route_that_never_called_the_function_fails(self):
        assert any(
            "never reached the function" in reason
            for reason in check.assess(
                _swap(
                    "[measured] direct route: 3 call(s), 3.81s, 441 tokens",
                    "[measured] direct route: 0 call(s), 3.81s, 441 tokens",
                )
            )
        )


class TestEverySkuHadToBeAskedFor:
    """No price is reachable any other way, so a SKU not asked for is a SKU invented."""

    def test_two_of_three_skus_fails(self):
        assert any(
            "a price it did not ask for" in reason
            for reason in check.assess(
                _swap(
                    "[measured] dispatches: 3 across 3 SKU(s)",
                    "[measured] dispatches: 2 across 2 SKU(s)",
                )
            )
        )

    def test_dispatching_nothing_fails(self):
        assert any(
            "road not taken" in reason or "did not ask for" in reason
            for reason in check.assess(
                _swap(
                    "[measured] dispatches: 3 across 3 SKU(s)",
                    "[measured] dispatches: 0 across 0 SKU(s)",
                )
            )
        )

    def test_more_dispatches_than_skus_is_fine(self):
        """A model may write a program that asks twice; that is its business, not a defect."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatches: 3 across 3 SKU(s)",
                    "[measured] dispatches: 6 across 3 SKU(s)",
                )
            )
            == []
        )

    def test_a_missing_dispatches_line_fails(self):
        assert any(
            "dispatches" in reason for reason in check.assess(_without("[measured] dispatches:"))
        )


class TestTheTwoStatementsOfOneVerdictMustAgree:
    """Act 4 restates what acts 2 and 3 measured. Restating is what makes disagreement visible."""

    def test_a_summary_that_contradicts_the_measurement_fails(self):
        assert any(
            "disagree" in reason
            for reason in check.assess(
                _swap(
                    "[measured] direct route reached the exact total:   False",
                    "[measured] direct route reached the exact total:   True",
                )
            )
        )

    def test_a_missing_summary_fails(self):
        assert any(
            "never restated" in reason
            for reason in check.assess(_without("dispatch route reached the exact total"))
        )


class TestAModelCannotAnswerForTheHost:
    """#314: the tag is the boundary between what ran and what was written about it."""

    def test_an_untagged_line_is_not_read(self):
        forged = _HEALTHY.replace(
            "The total cost is **$233.20**.",
            "The total cost is **$233.20**.\ndirect route: reply carries 218.15: True",
        )
        assert check.assess(forged) == []
        assert check.carried_direct(forged) is False

    def test_a_quoted_tagged_line_is_not_read_as_the_left_margin_one(self):
        """`quoted()` prefixes a model's tagged line with `> `, which the anchor rejects."""
        forged = _HEALTHY.replace(
            "The total cost is **$233.20**.",
            "> [measured] direct route: reply carries 218.15: True",
        )
        assert check.carried_direct(forged) is False

    def test_a_second_copy_of_a_line_is_refused_rather_than_resolved(self):
        doubled = _HEALTHY.replace(
            "  [measured] Disposed 1 sandbox(es).",
            "  [measured] Disposed 4 sandbox(es).\n  [measured] Disposed 1 sandbox(es).",
        )
        assert any("none of them can be trusted" in reason for reason in check.assess(doubled))


class TestTheMeasurementItself:
    def test_an_unordered_summary_fails(self):
        assert any(
            "not ordered" in reason
            for reason in check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.09s, median 1.09s, max 1.10s",
                    "[measured] round trip: 2 gap(s), min 1.50s, median 1.09s, max 1.10s",
                )
            )
        )

    def test_no_gaps_fails(self):
        assert any(
            "nothing was measured" in reason
            for reason in check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.09s, median 1.09s, max 1.10s",
                    "[measured] round trip: 0 gap(s), min 0.00s, median 0.00s, max 0.00s",
                )
            )
        )

    def test_a_missing_round_trip_line_fails(self):
        assert any("round trip" in reason for reason in check.assess(_without("round trip:")))

    def test_a_fast_round_trip_is_not_rejected(self):
        """Below the poll interval means calls overlapped, which the transport allows."""
        assert (
            check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.09s, median 1.09s, max 1.10s",
                    "[measured] round trip: 5 gap(s), min 0.00s, median 0.04s, max 1.13s",
                )
            )
            == []
        )

    def test_wall_clock_and_tokens_are_recorded_not_bounded(self):
        """A slow control plane is a finding, not a failing grade."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 3 call(s), 19.60s, 1762 tokens",
                    "[measured] dispatch route: 3 call(s), 240.00s, 99999 tokens",
                )
            )
            == []
        )


class TestTheBillableSandboxWentAway:
    def test_a_leaked_sandbox_fails(self):
        assert any(
            "bills until" in reason
            for reason in check.assess(
                _swap("[measured] Disposed 1 sandbox(es).", "[measured] Disposed 0 sandbox(es).")
            )
        )

    def test_a_missing_footer_fails(self):
        assert any("Disposed" in reason for reason in check.assess(_without("Disposed")))


class TestTheCommandLine:
    def test_a_healthy_file_exits_zero(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_HEALTHY, encoding="utf-8")
        assert check.main(["check", str(path)]) == 0
        assert "did not reach it" in capsys.readouterr().out

    def test_a_lucky_direct_route_is_named_in_the_success_line(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(
            _HEALTHY.replace(
                "[measured] direct route: reply carries 218.15: False",
                "[measured] direct route: reply carries 218.15: True",
            ).replace(
                "[measured] direct route reached the exact total:   False",
                "[measured] direct route reached the exact total:   True",
            ),
            encoding="utf-8",
        )
        assert check.main(["check", str(path)]) == 0
        assert "reached it too" in capsys.readouterr().out

    def test_a_broken_run_exits_one_and_says_why(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_without("Disposed"), encoding="utf-8")
        assert check.main(["check", str(path)]) == 1
        assert "FAIL" in capsys.readouterr().err

    def test_too_many_arguments_is_a_usage_error(self, capsys):
        assert check.main(["check", "a", "b"]) == 2
        assert "usage" in capsys.readouterr().err

    def test_stdin_is_read_when_no_path_is_given(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(_HEALTHY))
        assert check.main(["check"]) == 0


@pytest.mark.parametrize(
    "line",
    [
        "[measured] dispatches:",
        "[measured] dispatch route: 3 call(s)",
        "[measured] direct route: 3 call(s)",
        "round trip:",
        "Disposed",
    ],
)
def test_every_measured_line_is_load_bearing(line: str):
    """Removing any one of them fails the check, so none is decoration."""
    assert check.assess(_without(line)) != []
