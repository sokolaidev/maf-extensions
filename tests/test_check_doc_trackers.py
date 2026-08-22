"""Tests for the checker that reads a Status row's tracker against the live issue.

Every test here is offline. The script's one network call lives behind `ask`, and nothing below
touches it: what is worth pinning is the reading — which rows count, which references belong to
this repository, and which of the two mistakes each finding is. A test that reached GitHub would
fail on a train and prove nothing about the parsing that actually goes wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_doc_trackers import (  # noqa: E402
    Row,
    findings,
    is_outstanding,
    query,
    references,
    slug_from_url,
    status_rows,
)

_SLUG = "sokolaidev/maf-extensions"


def _link(number: int, kind: str = "issues") -> str:
    return f"[#{number}](https://github.com/{_SLUG}/{kind}/{number})"


def _row(state: str, tracking: str, decision: str = "A decision") -> Row:
    return Row(path="docs/sandbox/x.md", line=10, decision=decision, state=state, tracking=tracking)


class TestReadingTheTable:
    def test_it_finds_the_rows_under_the_status_heading(self):
        text = "# Doc\n\nProse.\n\n## Status\n\n| Decision | State | Tracking |\n|---|---|---|\n| A | open | none |\n"
        rows = status_rows(text, "docs/x.md")
        assert [(row.decision, row.state, row.tracking) for row in rows] == [("A", "open", "none")]

    def test_the_line_number_locates_the_row(self):
        """The report is only useful if a reader can open the file at the row."""
        text = "# Doc\n\n## Status\n\n| Decision | State | Tracking |\n|---|---|---|\n| A | open | x |\n"
        assert status_rows(text, "docs/x.md")[0].line == 7

    def test_a_document_with_no_status_table_yields_nothing(self):
        assert status_rows("# Doc\n\nProse only.\n", "docs/x.md") == []

    def test_it_stops_at_the_next_heading(self):
        text = "## Status\n\n| Decision | State | Tracking |\n|---|---|---|\n| A | open | x |\n\n## After\n\n| B | open | y |\n"
        assert [row.decision for row in status_rows(text, "docs/x.md")] == ["A"]

    def test_a_row_with_too_few_cells_is_left_to_the_structure_test(self):
        text = "## Status\n\n| Decision | State | Tracking |\n|---|---|---|\n| A | open |\n"
        assert status_rows(text, "docs/x.md") == []


class TestReadingTheReferences:
    def test_it_reads_an_issue_and_a_pull_request(self):
        found = references(f"{_link(477)} and {_link(532, 'pull')}")
        assert [(ref.number, ref.slug) for ref in found] == [(477, _SLUG), (532, _SLUG)]

    @pytest.mark.parametrize("word", ["open", "closed", "merged"])
    def test_it_reads_the_annotation_the_convention_writes(self, word: str):
        assert references(f"{_link(477)} ({word})")[0].claimed == word.upper()

    def test_a_group_annotation_attaches_to_the_reference_it_follows(self):
        """`(both open)` labels two; checking one of them is the safe direction to be wrong in."""
        found = references(f"{_link(567)} and {_link(568)} (both open)")
        assert [ref.claimed for ref in found] == [None, "OPEN"]

    def test_a_parenthetical_that_is_not_a_state_is_not_an_annotation(self):
        assert references(f"{_link(136)} (the symlink bug)")[0].claimed is None

    def test_another_repository_keeps_its_own_slug(self):
        cell = "upstream [x](https://github.com/microsoft/azure-container-apps/issues/1807) open"
        assert references(cell)[0].slug == "microsoft/azure-container-apps"

    def test_a_cell_naming_nothing_yields_nothing(self):
        assert references("untracked") == []


class TestWhichRowsAreOutstanding:
    @pytest.mark.parametrize("state", ["open", "partial — half of it", "parked", "Open"])
    def test_these_still_owe_something(self, state: str):
        assert is_outstanding(state)

    @pytest.mark.parametrize("state", ["shipped", "settled — nothing to build", "—"])
    def test_these_do_not(self, state: str):
        assert not is_outstanding(state)

    def test_a_shipped_row_whose_prose_says_open_is_not_outstanding(self):
        """The word appears in shipped rows describing what their successors still owe."""
        assert not is_outstanding("shipped — the umbrella's remaining parts are open")


class TestTheTwoMistakes:
    def test_an_annotation_that_disagrees_is_reported(self):
        rows = [_row("open", f"{_link(542)} (open)")]
        assert "names #542 as (open), and it is merged" in findings(rows, {542: "MERGED"}, _SLUG)[0]

    def test_an_annotation_that_agrees_is_not(self):
        rows = [_row("open", f"{_link(477)} (open)")]
        assert findings(rows, {477: "OPEN"}, _SLUG) == []

    def test_an_outstanding_row_whose_every_tracker_closed_is_reported(self):
        rows = [_row("open", _link(395))]
        problem = findings(rows, {395: "CLOSED"}, _SLUG)
        assert "every tracker it names has closed (#395)" in problem[0]

    def test_one_open_tracker_is_enough_to_keep_a_row_tracked(self):
        rows = [_row("open", f"{_link(395)} (closed), {_link(567)} (open)")]
        assert findings(rows, {395: "CLOSED", 567: "OPEN"}, _SLUG) == []

    def test_a_shipped_row_pinning_a_closed_tracker_is_fine(self):
        """Shipped work pins what delivered it, and that is closed by definition."""
        rows = [_row("shipped", f"{_link(532, 'pull')} (merged)")]
        assert findings(rows, {532: "MERGED"}, _SLUG) == []

    def test_another_repository_is_not_judged(self):
        cell = "upstream [x](https://github.com/microsoft/azure-container-apps/issues/1807) open"
        assert findings([_row("open", cell)], {}, _SLUG) == []

    def test_a_number_this_repository_does_not_have_is_reported(self):
        rows = [_row("open", _link(999999))]
        assert "#999999 does not exist" in findings(rows, {999999: None}, _SLUG)[0]

    def test_a_row_pinning_a_page_rather_than_an_issue_is_left_alone(self):
        """An index README pins through a document, which this check has no opinion about."""
        assert (
            findings(
                [_row("open", "[`../policy-isolation.md`](../policy-isolation.md)")], {}, _SLUG
            )
            == []
        )


class TestTheQuery:
    def test_it_asks_for_every_number_in_one_document(self):
        asked = query(_SLUG, [1, 2])
        assert "n1: issueOrPullRequest(number: 1)" in asked
        assert "n2: issueOrPullRequest(number: 2)" in asked

    def test_it_asks_the_kind_that_answers_for_both(self):
        """A tracking cell names merged pull requests as often as issues."""
        assert "... on Issue { state }" in query(_SLUG, [1])
        assert "... on PullRequest { state }" in query(_SLUG, [1])


class TestTheRemoteSlug:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/sokolaidev/maf-extensions.git",
            "https://github.com/sokolaidev/maf-extensions",
            "git@github.com:sokolaidev/maf-extensions.git",
            "  https://github.com/sokolaidev/maf-extensions/  ",
        ],
    )
    def test_it_reads_either_spelling(self, url: str):
        assert slug_from_url(url) == _SLUG

    def test_a_url_it_cannot_read_raises_rather_than_guessing(self):
        with pytest.raises(ValueError, match="cannot read an owner/name"):
            slug_from_url("not-a-remote")
