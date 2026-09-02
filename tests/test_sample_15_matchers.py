"""What `samples/15_acas_codeact_host_tools` will and will not accept as the answer.

Three matchers decide whether a program produced the table, and the live check's suite cannot
reach them — it only sees the counts they printed. A matcher that says `6 of 6` about a wrong
table makes every assertion above it agree with a run that did not happen.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

_AGENT = Path(__file__).resolve().parent.parent / "samples/15_acas_codeact_host_tools/agent.py"
_spec = importlib.util.spec_from_file_location("sample_15", _AGENT)
assert _spec is not None and _spec.loader is not None
sample = importlib.util.module_from_spec(_spec)

# Importing the sample *runs* it, and it leaves two marks on the process. It puts its own
# directory first on `sys.path`, the way `sys.path[0]` works when a sample is run as a script,
# and loading it caches what that resolved. Both are global and every sample carries a module
# named `_scaffold`, so a test module that keeps either answers a later sample's import with a
# copy that is not theirs — by name out of the cache, or by path off the front of `sys.path`.
# `test_sample_modules_import.py` restores both for this reason and fails when the cache is
# kept; nothing fails when the path is, which is why it is spelled out here.
_PATH_BEFORE = list(sys.path)
try:
    _spec.loader.exec_module(sample)
finally:
    sys.path[:] = _PATH_BEFORE
    for _name, _loaded in list(sys.modules.items()):
        _origin = getattr(_loaded, "__file__", None)
        if _origin and Path(_origin).parent == _AGENT.parent:
            del sys.modules[_name]


def test_importing_the_sample_left_nothing_behind():
    """Neither mark the import makes on the process outlives this module's import."""
    assert str(_AGENT.parent) not in sys.path
    assert [
        name
        for name, loaded in list(sys.modules.items())
        if (origin := getattr(loaded, "__file__", None)) and Path(origin).parent == _AGENT.parent
    ] == []


def test_act_five_reports_version_derived_cleanup_with_surviving_directories(monkeypatch, capsys):
    async def totals(_router, _thread, _registry):
        return 3, 2, 75, 25

    monkeypatch.setattr(sample, "_what_one_sandbox_holds", totals)
    asyncio.run(sample.act_five_what_the_runs_left_behind(object(), object()))

    output = capsys.readouterr().out
    assert sample.CALL_RECLAIMS
    assert "call directory cleanup: reclaimed by the framework" in output
    assert "call directories across both sandboxes: 6" in output


def _table(cells: dict[str, dict[str, float]], separator: str = "\t") -> str:
    """The table as a program prints it: one row per state and product, then a state total."""
    lines = ["state\tproduct\ttotal"]
    for state, products in cells.items():
        lines += [separator.join((state, name, str(value))) for name, value in products.items()]
        lines.append(separator.join((state, "TOTAL", f"{sum(products.values()):.2f}")))
    return "\n".join(lines)


_HONEST = _table(sample.TRUTH)
#: Every cell and both totals, and not one label. The check requires the rows and the product
#: names of the host-tool-call route only, so on the direct route this is the whole answer.
_UNLABELLED = "\n".join(
    ["\t".join(str(value) for value in products.values()) for products in sample.TRUTH.values()]
    + [f"{sum(products.values()):.2f}" for products in sample.TRUTH.values()]
)
#: Every value present, every one against the wrong state. The cells and both totals survive it.
_SWAPPED = _table(dict(zip(sample.TRUTH, reversed(list(sample.TRUTH.values())), strict=True)))


class TestFiguresIn:
    """The values, matched to the cent, however the program chose to write them."""

    def test_a_float_sum_matches_the_cell_it_is(self):
        """`1150.35 + 640.80` prints as `1791.1499999999999`, and that is the right answer."""
        assert sample.figures_in("Oregon\tGasket\t1791.1499999999999", [1791.15]) == 1

    @pytest.mark.parametrize("grouped", ["1,896.25", "1_896.25", "1896.25"])
    def test_either_grouping_matches(self, grouped: str):
        """Python writes a grouped number two ways and `f"{x:_}"` is the second of them."""
        assert sample.figures_in(f"| Washington | Widget | {grouped} |", [1896.25]) == 1

    def test_a_negated_value_does_not_match(self):
        """A table of correct magnitudes, every one negated, is not the answer."""
        assert sample.figures_in("Washington\tWidget\t-1896.25", [1896.25]) == 0

    def test_a_hyphen_in_a_label_is_not_a_sign(self):
        """`WA-1896.25` is a label and a value, not a negative one."""
        assert sample.figures_in("WA-1896.25", [1896.25]) == 1

    def test_a_value_that_is_out_by_a_cent_does_not_match(self):
        assert sample.figures_in("Washington\tWidget\t1896.24", [1896.25]) == 0

    @pytest.mark.parametrize("printed", ["1.79115e3", "1.79115e+03", "1.79115E3"])
    def test_an_exponent_spells_the_value_it_is(self, printed: str):
        """How the output is formatted is the model's to choose, and this is one of the ways."""
        assert sample.figures_in(f"Oregon\tGasket\t{printed}", [1791.15]) == 1

    def test_a_separator_python_itself_would_reject_is_still_read(self):
        """`float("1__791.15")` raises, and the separators are dropped before the parse."""
        assert sample.figures_in("Oregon	Gasket	1__791.15", [1791.15]) == 1

    def test_a_run_id_is_not_a_number(self):
        """`1e3f4a2b9c0d` starts with something an exponent rule would read as a thousand."""
        assert sample.figures_in("run 1e3f4a2b9c0d finished", [1000.0]) == 0


class TestRowsIn:
    """A value is only an answer when it is attached to the right state and product."""

    def test_the_honest_table_has_every_row(self):
        assert sample.rows_in(_HONEST) == len(sample.PRODUCT_CELLS)

    def test_the_swapped_table_has_none(self):
        """The check the rows exist for: the cells and the totals cannot see this."""
        assert sample.figures_in(_SWAPPED, sample.PRODUCT_CELLS) == len(sample.PRODUCT_CELLS)
        assert sample.figures_in(_SWAPPED, sample.STATE_TOTALS.values()) == len(sample.STATE_TOTALS)
        assert sample.rows_in(_SWAPPED) == 0

    @pytest.mark.parametrize("separator", ["\t", " | "])
    def test_a_row_matches_whatever_it_is_separated_by(self, separator: str):
        assert sample.rows_in(_table(sample.TRUTH, separator)) == len(sample.PRODUCT_CELLS)

    def test_a_line_carrying_a_whole_state_is_not_three_rows(self):
        """One line holding all three of a state's pairs is one row, not three."""
        two_lines = "\n".join(
            state + "".join(f"\t{name}\t{value}" for name, value in products.items())
            for state, products in sample.TRUTH.items()
        )
        assert sample.figures_in(two_lines, sample.PRODUCT_CELLS) == len(sample.PRODUCT_CELLS)
        assert sample.rows_in(two_lines) == 0

    def test_a_table_grouped_by_state_matches(self):
        """The state on a header line and the rows beneath it, which live runs have printed."""
        grouped = "\n".join(
            line
            for state, products in sample.TRUTH.items()
            for line in (state, *(f"  {name} {value}" for name, value in products.items()))
        )
        assert sample.rows_in(grouped) == len(sample.PRODUCT_CELLS)


class _Content:
    def __init__(self, arguments: str) -> None:
        self.arguments = arguments


class _Message:
    def __init__(self, *arguments: str) -> None:
        self.contents = [_Content(one) for one in arguments]


class _Response:
    def __init__(self, *messages: _Message) -> None:
        self.messages = list(messages)


class TestAmountsTheModelWrote:
    """A figure the model carried is a value it wrote down, not a string that appears."""

    def test_a_whole_amount_written_as_an_integer_counts(self):
        """`980.00` is the figure; `980` is the model writing it into its program."""
        assert sample.amounts_the_model_wrote(_Response(_Message('{"code": "x = [980]"}'))) == 1

    @pytest.mark.parametrize(
        "spelling", ["980", "980.0", "980.00", "980.000", "9.8e2", "9.8E+2", "98e1"]
    )
    def test_every_spelling_of_one_amount_is_one_figure(self, spelling: str):
        written = _Response(_Message('{"code": "x = [' + spelling + ']"}'))
        assert sample.amounts_the_model_wrote(written) == 1

    @pytest.mark.parametrize("longer", ["1980.0", "1088.1", "112.05"])
    def test_a_number_that_merely_contains_one_is_not_that_figure(self, longer: str):
        """`1980.0` holds the characters of `980.0` and is a different number."""
        assert sample.amounts_the_model_wrote(_Response(_Message('{"x": ' + longer + "}"))) == 0

    @pytest.mark.parametrize("spelling", ["1240.50", "1_240.50", "1_240.5"])
    def test_a_python_separator_is_the_figure_it_separates(self, spelling: str):
        """`code` carries Python, and `1_240.50` is a literal a model may reasonably write."""
        written = _Response(_Message('{"code": "x = [' + spelling + ']"}'))
        assert sample.amounts_the_model_wrote(written) == 1

    def test_a_literal_python_itself_would_reject_is_still_read(self):
        """`float("1_240_.50")` raises, and a model writing it has still written the figure."""
        written = _Response(_Message('{"code": "x = [1_240_.50]"}'))
        assert sample.amounts_the_model_wrote(written) == 1

    def test_a_number_that_is_only_the_front_of_a_token_is_not_a_figure(self):
        """`980abc` is not the model writing 980 down, and the exponent rule must not make it one."""
        assert sample.amounts_the_model_wrote(_Response(_Message('{"x": "980abc"}'))) == 0

    def test_an_identifier_is_not_a_figure(self):
        """The digits of `PRD-1` and `STO-202` are names, and no amount is worth 202."""
        arguments = "{\"code\": \"store_sales('STO-202'); product_name('PRD-1')\"}"
        assert sample.amounts_the_model_wrote(_Response(_Message(arguments))) == 0

    def test_the_count_is_distinct_figures_across_every_call(self):
        first, second = _Message('{"x": 12.05}'), _Message('{"x": 12.05}', '{"y": 47.9}')
        assert sample.amounts_the_model_wrote(_Response(first, second)) == 2

    def test_a_response_with_no_tool_calls_carried_nothing(self):
        assert sample.amounts_the_model_wrote(_Response()) == 0


class TestLedgerRunAttribution:
    def test_same_run_gaps_are_transport_and_cross_run_gaps_are_boundaries(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ledger = sample.Ledger()
        first = sample.HostToolRun(sample.HostToolRegistry())
        second = sample.HostToolRun(sample.HostToolRegistry())
        clock = iter([0.0, 1.0, 2.0, 3.0, 10.0, 11.0])
        monkeypatch.setattr(sample.time, "perf_counter", lambda: next(clock))

        with sample.observe_host_tool_call(first, "state_id"):
            ledger.arriving("state_id(Washington)")
            ledger.answered()
        with sample.observe_host_tool_call(first, "stores_in_state"):
            ledger.arriving("stores_in_state(ST-WA)")
            ledger.answered()
        with sample.observe_host_tool_call(second, "store_sales"):
            ledger.arriving("store_sales(STO-101)")
            ledger.answered()

        trips, boundaries = ledger.round_trips()
        assert trips == [1.0]
        assert boundaries == [7.0]
        assert ledger.runs_that_called_a_host_tool == {first.run_id, second.run_id}

    def test_an_unobserved_call_does_not_count_as_a_run_that_called_a_host_tool(self):
        ledger = sample.Ledger()
        ledger.arriving("state_id(Washington)")
        assert ledger.runs_that_called_a_host_tool == set()


class TestTheConversationIds:
    """One conversation per route and per run: `dispose_scope` deletes by label, not by owner."""

    def test_each_route_and_each_run_gets_its_own(self):
        assert sample.HOST_TOOL_CALL_THREAD != sample.DIRECT_THREAD
        expected = os.environ.get("GITHUB_RUN_ID") or f"local-{os.getpid()}"
        assert expected in sample.HOST_TOOL_CALL_THREAD
        assert expected in sample.DIRECT_THREAD

    def test_both_fit_the_label_the_backend_writes(self):
        """Over `_LABEL_VALUE_MAX` (63) an ACAS label becomes a digest, which is unreadable."""
        assert max(len(sample.HOST_TOOL_CALL_THREAD), len(sample.DIRECT_THREAD)) <= 63


class TestTheProgramThatAnswered:
    """One table, from one program, because that is what the walk was asked for."""

    def test_two_halves_do_not_add_up_to_a_table(self):
        halves = [_table({state: products}) for state, products in sample.TRUTH.items()]
        picked = sample.the_program_that_answered(halves)
        assert sample.figures_in(picked, sample.PRODUCT_CELLS) == len(sample.PRODUCT_CELLS) // 2
        assert sample.figures_in("\n".join(halves), sample.PRODUCT_CELLS) == len(
            sample.PRODUCT_CELLS
        )

    def test_the_table_wins_against_the_same_values_in_the_wrong_places(self):
        """Two programs can hold the same six values with only one of them a table."""
        assert sample.the_program_that_answered([_SWAPPED, _HONEST]) == _HONEST
        assert sample.the_program_that_answered([_HONEST, _SWAPPED]) == _HONEST

    def test_a_complete_answer_beats_a_labelled_fragment(self):
        """Only the host-tool-call route must label its table, so an unlabelled one has still answered."""
        probe = "Washington\tWidget\t1896.25"
        assert sample.the_program_that_answered([probe, _UNLABELLED]) == _UNLABELLED
        assert sample.the_program_that_answered([_UNLABELLED, probe]) == _UNLABELLED

    def test_the_rows_break_the_tie_underneath_the_cells(self):
        """The honest and swapped tables agree on everything the cells and totals can see."""
        honest, swapped = sample.graded(_HONEST), sample.graded(_SWAPPED)
        assert honest[:2] == swapped[:2]
        assert honest > swapped

    def test_the_program_with_the_table_is_the_one_scored(self):
        """A probe that printed nothing does not displace the program that answered."""
        assert sample.the_program_that_answered(["3.13.1\n", _HONEST]) == _HONEST

    def test_no_results_is_not_an_error(self):
        assert sample.the_program_that_answered([]) == ""


class TestTheCallDirectoryFilter:
    """Act 5 counts what a sandbox kept, and it counts by matching the directory's name.

    A name shape the filter does not recognise is not an error anywhere — that call is dropped
    from the count and the act reports fewer than are there, which only a live run would show.
    """

    @pytest.mark.parametrize(
        "name",
        [uuid4().hex, uuid4().hex[:12]],
        ids=["a whole uuid", "the twelve characters an older core wrote"],
    )
    def test_a_call_directory_is_recognised(self, name: str):
        assert sample._CALL_ID.fullmatch(name)

    @pytest.mark.parametrize(
        "name",
        ["host_tools", "work", "outputs", uuid4().hex[:11], uuid4().hex[:20], "g" * 12],
        ids=["the transport's", "the work dir", "a plain name", "too short", "between", "not hex"],
    )
    def test_anything_else_is_not(self, name: str):
        assert sample._CALL_ID.fullmatch(name) is None
