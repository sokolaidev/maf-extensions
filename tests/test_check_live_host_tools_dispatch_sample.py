"""What `scripts/check_live_host_tools_dispatch_sample.py` will and will not let through.

`_HEALTHY` is a real run of `samples/15_acas_codeact_host_tools` against a live ACAS sandbox
and a live model, verbatim apart from the model's own replies — those are prose and vary, and
standing in for them is the point of the tag the check keys on.

The suite is organised around the asymmetry that makes this check unusual. **One** line is
enforced: that the program printed the exact total, read from the framework's record of what
`execute_code` returned, and so an interpreter's output. Everything else — whether the model
relayed it, and what either no-sandbox route made of the same prices — is recorded.

Both halves are tested. An implementation that enforced a reply verdict would pass every
happy-path test here and go red on a real run: one has already been seen where the program
printed `218.15` and the model's reply said `239.75`.
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


#: A real run. The model's three replies are the only edited lines.
_HEALTHY = """== 1. What the host wired ==

  registered:            unit_price
  identities the spec carries: ['app']
  Reading the aggregate sealed the registry: a later register() is refused, because
  this is the moment the surface became a policy the router can match.

== 2. A program that cannot answer without calling out ==

218.15

  [measured] dispatches: 3 across 3 SKU(s)
  [measured] dispatch route: 3 call(s), 14.76s, 1928 tokens
  [measured] dispatch route: reply carries 218.15: True
  [measured] dispatch route: the program printed 218.15: True
  [measured] round trip: 2 gap(s), min 1.10s, median 1.14s, max 1.18s

== 3. The same question, answered without a sandbox ==

  Asked twice: once for the total alone, once for the working first.

The total cost is **$213.35**.

  [measured] one-pass route: 3 call(s), 2.85s, 441 tokens
  [measured] one-pass route: reply carries 218.15: False

3 x 41.75 = 125.25
7 x 12.40 = 86.80
2 x 3.05 = 6.10

125.25 + 86.80 + 6.10 = 218.15

Total cost: **218.15**

  [measured] shown-working route: 3 call(s), 3.20s, 561 tokens
  [measured] shown-working route: reply carries 218.15: True

== 4. What the round trips bought ==

  The order costs 218.15. All three routes were given the same prices.

  [measured] the sandbox computed the exact total:        True
  [measured] dispatch route reached the exact total:      True
  [measured] one-pass route reached the exact total:      False
  [measured] shown-working route reached the exact total: True

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

    def test_the_route_that_landed_alongside_is_named(self):
        assert check.unaided_routes_that_landed(_HEALTHY) == ["shown-working route"]


class TestOnlyTheProgramsOutputIsEnforced:
    """The single hard assertion, read from what `execute_code` returned."""

    def test_a_program_that_did_not_print_the_total_fails(self):
        broken = _swap(
            "[measured] dispatch route: the program printed 218.15: True",
            "[measured] dispatch route: the program printed 218.15: False",
        ).replace(
            "[measured] the sandbox computed the exact total:        True",
            "[measured] the sandbox computed the exact total:        False",
        )
        assert any("did not print the exact total" in r for r in check.assess(broken))

    def test_a_missing_program_line_fails(self):
        assert any(
            "the program printed" in r for r in check.assess(_without("the program printed"))
        )

    def test_the_summary_of_it_has_to_agree(self):
        assert any(
            "disagree" in r
            for r in check.assess(
                _swap(
                    "[measured] the sandbox computed the exact total:        True",
                    "[measured] the sandbox computed the exact total:        False",
                )
            )
        )

    def test_a_missing_summary_of_it_fails(self):
        assert any(
            "never restated whether the sandbox" in r
            for r in check.assess(_without("the sandbox computed the exact total"))
        )


class TestEveryReplyVerdictIsRecordedNotRequired:
    """Three routes, three reply verdicts, none of them enforced.

    The dispatch reply is in here too, and that is the correction this suite exists to pin: a
    run has been seen where the program printed the total and the model's reply did not.
    """

    def test_a_dispatch_reply_that_lost_the_total_is_not_a_failure(self):
        """Seen live. The sandbox is not at fault for what the model said afterwards."""
        drifted = _swap(
            "[measured] dispatch route: reply carries 218.15: True",
            "[measured] dispatch route: reply carries 218.15: False",
        ).replace(
            "[measured] dispatch route reached the exact total:      True",
            "[measured] dispatch route reached the exact total:      False",
        )
        assert check.assess(drifted) == []

    def test_a_one_pass_route_that_got_it_right_is_not_a_failure(self):
        lucky = _swap(
            "[measured] one-pass route: reply carries 218.15: False",
            "[measured] one-pass route: reply carries 218.15: True",
        ).replace(
            "[measured] one-pass route reached the exact total:      False",
            "[measured] one-pass route reached the exact total:      True",
        )
        assert check.assess(lucky) == []
        assert "one-pass route" in check.unaided_routes_that_landed(lucky)

    def test_a_shown_working_route_that_got_it_wrong_is_not_a_failure(self):
        unlucky = _swap(
            "[measured] shown-working route: reply carries 218.15: True",
            "[measured] shown-working route: reply carries 218.15: False",
        ).replace(
            "[measured] shown-working route reached the exact total: True",
            "[measured] shown-working route reached the exact total: False",
        )
        assert check.assess(unlucky) == []
        assert check.unaided_routes_that_landed(unlucky) == []

    @pytest.mark.parametrize("route", ["dispatch route", "one-pass route", "shown-working route"])
    def test_every_route_still_has_to_be_reported(self, route: str):
        assert check.assess(_without(f"{route}: reply carries")) != []

    @pytest.mark.parametrize("route", ["dispatch route", "one-pass route", "shown-working route"])
    def test_a_route_that_never_called_the_function_fails(self, route: str):
        line = [r for r in _HEALTHY.splitlines() if f"{route}: " in r and "call(s)" in r][0]
        assert any(
            "never reached the function" in r
            for r in check.assess(_swap(line, line.replace("3 call(s)", "0 call(s)")))
        )


class TestEverySkuHadToBeAskedFor:
    """No price is reachable any other way, so a SKU not asked for is a SKU invented."""

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
        assert any("dispatches" in r for r in check.assess(_without("[measured] dispatches:")))


class TestAModelCannotAnswerForTheHost:
    """#314: the tag at the left margin is the boundary between what ran and what was said."""

    def test_an_untagged_line_is_not_read(self):
        forged = _HEALTHY.replace(
            "The total cost is **$213.35**.",
            "The total cost is **$213.35**.\none-pass route: reply carries 218.15: True",
        )
        assert check.assess(forged) == []
        assert "one-pass route" not in check.unaided_routes_that_landed(forged)

    def test_a_quoted_tagged_line_is_not_read_as_the_left_margin_one(self):
        """`quoted()` prefixes a model's tagged line with `> `, which the anchor rejects."""
        forged = _HEALTHY.replace(
            "The total cost is **$213.35**.",
            "> [measured] one-pass route: reply carries 218.15: True",
        )
        assert "one-pass route" not in check.unaided_routes_that_landed(forged)

    def test_a_quoted_program_line_cannot_stand_in_for_the_real_one(self):
        """The forgery that matters most, because that line is the only enforced one.

        The real line is removed and a quoted one put in the model's prose, so the duplicate
        rule cannot be what rejects it — only the left-margin anchor can. Without the anchor
        the check reads the model's claim as the sandbox's own and passes the run.
        """
        forged = _without("the program printed").replace(
            "218.15\n",
            "218.15\n> [measured] dispatch route: the program printed 218.15: True\n",
            1,
        )
        assert any("the program printed" in r for r in check.assess(forged))

    def test_a_forged_program_line_alongside_the_real_one_is_refused(self):
        doubled = _HEALTHY.replace(
            "  [measured] dispatch route: the program printed 218.15: True",
            "  [measured] dispatch route: the program printed 218.15: False\n"
            "  [measured] dispatch route: the program printed 218.15: True",
        )
        assert any("none of them can be trusted" in r for r in check.assess(doubled))

    def test_a_second_copy_of_a_line_is_refused_rather_than_resolved(self):
        doubled = _HEALTHY.replace(
            "  [measured] Disposed 1 sandbox(es).",
            "  [measured] Disposed 4 sandbox(es).\n  [measured] Disposed 1 sandbox(es).",
        )
        assert any("none of them can be trusted" in r for r in check.assess(doubled))


class TestTheMeasurementItself:
    def test_an_unordered_summary_fails(self):
        assert any(
            "not ordered" in r
            for r in check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.10s, median 1.14s, max 1.18s",
                    "[measured] round trip: 2 gap(s), min 1.50s, median 1.14s, max 1.18s",
                )
            )
        )

    def test_no_gaps_fails(self):
        assert any(
            "nothing was measured" in r
            for r in check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.10s, median 1.14s, max 1.18s",
                    "[measured] round trip: 0 gap(s), min 0.00s, median 0.00s, max 0.00s",
                )
            )
        )

    def test_a_missing_round_trip_line_fails(self):
        assert any("round trip" in r for r in check.assess(_without("round trip:")))

    def test_a_fast_round_trip_is_not_rejected(self):
        """Below the poll interval means calls overlapped, which the transport allows."""
        assert (
            check.assess(
                _swap(
                    "[measured] round trip: 2 gap(s), min 1.10s, median 1.14s, max 1.18s",
                    "[measured] round trip: 2 gap(s), min 0.00s, median 0.02s, max 0.04s",
                )
            )
            == []
        )

    def test_wall_clock_and_tokens_are_recorded_not_bounded(self):
        """A slow control plane is a finding, not a failing grade."""
        assert (
            check.assess(
                _swap(
                    "[measured] dispatch route: 3 call(s), 14.76s, 1928 tokens",
                    "[measured] dispatch route: 3 call(s), 240.00s, 99999 tokens",
                )
            )
            == []
        )


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
    def test_a_healthy_file_exits_zero_and_names_what_landed(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(_HEALTHY, encoding="utf-8")
        assert check.main(["check", str(path)]) == 0
        assert "shown-working route" in capsys.readouterr().out

    def test_neither_unaided_route_landing_is_still_a_pass(self, tmp_path: Path, capsys):
        path = tmp_path / "out.txt"
        path.write_text(
            _HEALTHY.replace(
                "[measured] shown-working route: reply carries 218.15: True",
                "[measured] shown-working route: reply carries 218.15: False",
            ).replace(
                "[measured] shown-working route reached the exact total: True",
                "[measured] shown-working route reached the exact total: False",
            ),
            encoding="utf-8",
        )
        assert check.main(["check", str(path)]) == 0
        assert "neither route without one" in capsys.readouterr().out

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
        "the program printed",
        "the sandbox computed",
        "[measured] dispatch route: 3 call(s)",
        "[measured] one-pass route: 3 call(s)",
        "[measured] shown-working route: 3 call(s)",
        "round trip:",
        "Disposed",
    ],
)
def test_every_measured_line_is_load_bearing(line: str):
    """Removing any one of them fails the check, so none is decoration."""
    assert check.assess(_without(line)) != []
