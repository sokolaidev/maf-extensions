"""What `samples/15_acas_codeact_host_tools` will and will not accept as the answer.

The sample reads the framework's record of what `execute_code` returned and decides from it
whether a program produced the table. Three matchers do that, and none of them can be reached
by the live check's suite, which only ever sees the counts they printed. They are covered here
because the counts are what a release is graded on: a matcher that says `6 of 6` about a wrong
table makes every assertion above it agree with a run that did not happen.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
    """Neither mark the import makes on the process outlives this module's import.

    Asserted rather than inferred from a later sample passing: the shadowing is silent, and the
    test that would notice runs the samples in a fixed order this one does not control.
    """
    assert str(_AGENT.parent) not in sys.path
    assert [
        name
        for name, loaded in list(sys.modules.items())
        if (origin := getattr(loaded, "__file__", None)) and Path(origin).parent == _AGENT.parent
    ] == []


def _table(cells: dict[str, dict[str, float]], separator: str = "\t") -> str:
    """The table as a program prints it: one row per state and product, then a state total."""
    lines = ["state\tproduct\ttotal"]
    for state, products in cells.items():
        lines += [separator.join((state, name, str(value))) for name, value in products.items()]
        lines.append(separator.join((state, "TOTAL", f"{sum(products.values()):.2f}")))
    return "\n".join(lines)


_HONEST = _table(sample.TRUTH)
#: Every value present, every one against the wrong state. The cells and both totals survive it.
_SWAPPED = _table(dict(zip(sample.TRUTH, reversed(list(sample.TRUTH.values())), strict=True)))


class TestFiguresIn:
    """The values, matched to the cent, however the program chose to write them."""

    def test_a_float_sum_matches_the_cell_it_is(self):
        """`1150.35 + 640.80` prints as `1791.1499999999999`, and that is the right answer."""
        assert sample.figures_in("Oregon\tGasket\t1791.1499999999999", [1791.15]) == 1

    def test_a_thousands_separator_matches(self):
        assert sample.figures_in("| Washington | Widget | 1,896.25 |", [1896.25]) == 1

    def test_a_negated_value_does_not_match(self):
        """A table of correct magnitudes, every one negated, is not the answer.

        The sign is part of the number. Without it the matcher reads the magnitude and reports
        a program that got every total backwards as having printed the table.
        """
        assert sample.figures_in("Washington\tWidget\t-1896.25", [1896.25]) == 0

    def test_a_hyphen_in_a_label_is_not_a_sign(self):
        """`WA-1896.25` is a label and a value, not a negative one."""
        assert sample.figures_in("WA-1896.25", [1896.25]) == 1

    def test_a_value_that_is_out_by_a_cent_does_not_match(self):
        assert sample.figures_in("Washington\tWidget\t1896.24", [1896.25]) == 0


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
        """One line per state, each with all three pairs on it, is two rows and not six.

        Every cell scans every line, so without the one-product rule the same line answers for
        Widget, Gasket and Flange at once and a two-line output scores `6 of 6`.
        """
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


class TestTheProgramThatAnswered:
    """One table, from one program, because that is what the walk was asked for."""

    def test_two_halves_do_not_add_up_to_a_table(self):
        halves = [_table({state: products}) for state, products in sample.TRUTH.items()]
        picked = sample.the_program_that_answered(halves)
        assert sample.figures_in(picked, sample.PRODUCT_CELLS) == len(sample.PRODUCT_CELLS) // 2
        assert sample.figures_in("\n".join(halves), sample.PRODUCT_CELLS) == len(
            sample.PRODUCT_CELLS
        )

    def test_the_program_with_the_table_is_the_one_scored(self):
        """A probe that printed nothing does not displace the program that answered."""
        assert sample.the_program_that_answered(["3.13.1\n", _HONEST]) == _HONEST

    def test_no_results_is_not_an_error(self):
        assert sample.the_program_that_answered([]) == ""
