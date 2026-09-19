"""What `scripts/check_implicit_concatenation.py` reports, and what it must leave alone.

The defect is a comma left out between two literals in a collection: Python joins them, the
collection is one element shorter, and the program runs. The other half of this suite is the
deliberate form — a long message wrapped to fit the line — because a check that reports those
is a check somebody switches off.

Two boundaries carry the most weight. Parentheses are the whole vocabulary for "one value was
meant", so they must be believed; and `ast` columns count bytes where the tokenizer counts
characters, so a line holding any non-ASCII text is where a span goes wrong.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_implicit_concatenation", _ROOT / "scripts" / "check_implicit_concatenation.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


@pytest.fixture
def repo(tmp_path: Path):
    """A real git repository, because the script scans what `git ls-files` returns."""

    def _write(files: dict[str, str]) -> Path:
        for name, text in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        return tmp_path

    return _write


class TestTheDefectItExistsFor:
    """The shapes a missing comma leaves behind, which read as one element and were two."""

    def test_a_program_argument_built_from_two_literals_is_reported(self):
        source = (
            "run([\n"
            "    sys.executable,\n"
            '    "-c",\n'
            '    "import sys; print(1)"\n'
            '    "print(2)",\n'
            "])\n"
        )
        assert [line.split(":")[1] for line in check.findings(source, "x.py")] == ["4"]

    def test_the_only_element_of_a_list_is_reported_too(self):
        """Wider than CodeQL's query, which wants a second string element in the list.

        A two-element list that lost its one comma has no second element left to find, so the
        shape the defect produces is exactly the one that exemption would pass.
        """
        source = 'problems = [\n    "first problem"\n    "second problem",\n]\n'
        assert len(check.findings(source, "x.py")) == 1

    def test_a_tuple_element_is_reported(self):
        source = 'pair = (\n    "left"\n    "right",\n    "other",\n)\n'
        assert "one tuple element" in check.findings(source, "x.py")[0]

    def test_a_set_element_is_reported(self):
        source = 'names = {\n    "left"\n    "right",\n    "other",\n}\n'
        assert "one set element" in check.findings(source, "x.py")[0]

    def test_the_report_counts_the_literals_that_merged(self):
        source = 'lines = [\n    "one "\n    "two "\n    "three",\n]\n'
        assert "3 adjacent literals" in check.findings(source, "x.py")[0]

    def test_an_f_string_joined_to_a_plain_string_is_reported(self):
        """The `ast` node is a `JoinedStr` rather than a `Constant`, and the ambiguity is the same."""
        source = 'lines = [\n    f"{count} found "\n    "in the reply",\n    "other",\n]\n'
        assert len(check.findings(source, "x.py")) == 1

    def test_bytes_literals_are_reported(self):
        source = 'blobs = [\n    b"first"\n    b"second",\n    b"other",\n]\n'
        assert len(check.findings(source, "x.py")) == 1

    def test_an_element_that_is_an_expression_is_reported(self):
        """The element need not *be* the literals — a `+` chain ending in two of them is the same."""
        source = 'lines = [\n    "start " + name\n    + "first half "\n    "second half",\n]\n'
        assert [line.split(":")[1] for line in check.findings(source, "x.py")] == ["3"]

    def test_a_collection_inside_a_replacement_field_is_reported(self):
        """A list written inside `f"{…}"` loses a comma the same way any other one does."""
        source = "shown = [\n    f\"{['first' 'second']}\",\n]\n"
        assert len(check.findings(source, "x.py")) == 1


class TestWhatItLeavesAlone:
    """Every deliberate form. Each of these exists in this repository, which is the point."""

    def test_parentheses_say_one_value_was_meant(self):
        source = 'problems = [\n    (\n        "first half "\n        "second half"\n    ),\n]\n'
        assert check.findings(source, "x.py") == []

    def test_elements_that_kept_their_commas(self):
        source = 'names = [\n    "first",\n    "second",\n]\n'
        assert check.findings(source, "x.py") == []

    def test_a_triple_quoted_string_spanning_lines_is_one_literal(self):
        source = 'blocks = [\n    """first\nsecond""",\n    "other",\n]\n'
        assert check.findings(source, "x.py") == []

    def test_a_literal_continued_over_a_backslash_is_one_literal(self):
        """Two lines, one literal, and no newline in the value — which is not a concatenation."""
        source = 'lines = [\n    "first \\\n    second",\n    "other",\n]\n'
        assert check.findings(source, "x.py") == []

    def test_a_dict_value_may_be_written_in_parts(self):
        """A comma lost between two pairs puts a second `:` in one, which does not parse."""
        source = 'mapping = {\n    "key": "first half "\n    "second half",\n}\n'
        assert check.findings(source, "x.py") == []

    def test_a_call_argument_may_be_written_in_parts(self):
        source = 'print(\n    "first half "\n    "second half",\n    file=sys.stderr,\n)\n'
        assert check.findings(source, "x.py") == []

    def test_a_call_that_is_itself_an_element_keeps_that_freedom(self):
        """The nearest comma-separated container owns the run, and here that is the call.

        Most of this repository's wrapped messages sit here; reading them as elements of the
        enclosing list is what would make the check unusable.
        """
        source = 'cases = [\n    case(\n        "first half "\n        "second half",\n    ),\n]\n'
        assert check.findings(source, "x.py") == []

    def test_a_dict_nested_in_a_list_keeps_it_too(self):
        source = 'rows = [\n    {"key": "first half "\n     "second half"},\n    "other",\n]\n'
        assert check.findings(source, "x.py") == []

    def test_a_literal_inside_a_replacement_field_is_not_a_second_part(self):
        source = 'lines = [\n    f"{mapping[\'key\']} was read",\n    "other",\n]\n'
        assert check.findings(source, "x.py") == []


class TestWhatItCannotReach:
    """The one shape no rule here can see, asserted so the boundary is checkable."""

    def test_a_two_element_tuple_that_lost_its_only_comma(self):
        """What believing the parentheses costs, and the trailing comma that gets it back.

        `("left" "right")` is a parenthesized string, character for character the form that
        says one value was meant, so no reading of the source separates the two.
        """
        assert check.findings('pair = ("left" "right")\n', "x.py") == []
        assert len(check.findings('pair = ("left" "right",)\n', "x.py")) == 1


class TestSpansOnALineHoldingNonAscii:
    """`ast` columns are UTF-8 byte offsets; the tokenizer's are characters."""

    def test_an_element_does_not_swallow_the_ones_after_it(self):
        source = 'values = ["１６０", "a", "b", "c"]\n'
        assert check.findings(source, "x.py") == []

    def test_a_merge_on_such_a_line_is_still_reported(self):
        source = 'notes = [\n    "prêt — first half "\n    "second half",\n    "other",\n]\n'
        assert [line.split(":")[1] for line in check.findings(source, "x.py")] == ["2"]


class TestTheCommandLine:
    def test_a_tracked_file_that_merged_two_literals_fails_the_run(self, repo, monkeypatch, capsys):
        root = repo({"pkg/thing.py": 'names = [\n    "first"\n    "second",\n    "third",\n]\n'})
        monkeypatch.setattr(check, "repo_root", lambda: root)
        assert check.main(["check"]) == 1
        assert "pkg/thing.py:2" in capsys.readouterr().err

    def test_a_tree_that_hides_no_missing_comma_passes(self, repo, monkeypatch):
        root = repo({"pkg/thing.py": 'names = [\n    "first",\n    "second",\n]\n'})
        monkeypatch.setattr(check, "repo_root", lambda: root)
        assert check.main(["check"]) == 0

    def test_an_untracked_file_is_never_read(self, repo, monkeypatch):
        root = repo({"pkg/thing.py": "names = []\n"})
        (root / "scratch.py").write_text('names = [\n    "a"\n    "b",\n    "c",\n]\n', "utf-8")
        monkeypatch.setattr(check, "repo_root", lambda: root)
        assert check.main(["check"]) == 0

    def test_a_file_that_does_not_parse_is_reported_rather_than_skipped(
        self, repo, monkeypatch, capsys
    ):
        """A file nothing could read is a file this check did not cover, so it fails loudly."""
        root = repo({"pkg/thing.py": "def broken(:\n"})
        monkeypatch.setattr(check, "repo_root", lambda: root)
        assert check.main(["check"]) == 1
        assert "could not be read as Python" in capsys.readouterr().err

    def test_an_argument_is_refused(self, capsys):
        assert check.main(["check", "--all"]) == 2
        assert "usage" in capsys.readouterr().err


class TestThisRepository:
    def test_no_collection_literal_in_the_tree_hides_a_missing_comma(self):
        """The check over its own repository, so `pytest` alone reports what the gate would.

        Every deliberate concatenation here is parenthesized, and this is what holds the next
        one to that form.
        """
        problems: list[str] = []
        for path in check.tracked_python(_ROOT):
            source = path.read_text(encoding="utf-8")
            problems.extend(check.findings(source, path.relative_to(_ROOT).as_posix()))
        assert problems == []

    def test_the_concatenations_here_are_read_rather_than_skipped(self):
        """A scan that reaches nothing reports nothing, and reads as green either way.

        The test above is only worth what it covers, so the tree's deliberate concatenations
        are counted: a walk that stopped finding them, or a file list that came back empty,
        fails here instead of passing quietly.
        """
        seen = [
            found
            for path in check.tracked_python(_ROOT)
            for found in check.concatenations(path.read_text(encoding="utf-8"))
        ]
        assert len(seen) > 20, f"only {len(seen)} concatenations read in the whole tree"
