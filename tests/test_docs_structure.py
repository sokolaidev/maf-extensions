"""The documentation conventions `docs/AUTHORING.md` states, checked against the tree.

Offline by construction: every assertion reads files, so a moved document fails here rather
than surviving as a dead link a reader finds later.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_doc_paths", _ROOT / "scripts" / "check_doc_paths.py"
)
assert _spec and _spec.loader
_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_check)
_DOCS = _ROOT / "docs"
_SANDBOX = _DOCS / "sandbox"
_RESEARCH = _SANDBOX / "research"

_LINK = re.compile(r"\]\(\s*<?([^)>\s]+)>?[^)]*\)")
_ABSOLUTE = ("http://", "https://", "mailto:")
_OLD_BANNERS = ("Status: PROPOSED", "Status: AS BUILT", "Status: IMPLEMENTED")
_ISSUE_LINK = re.compile(r"https://github\.com/[^/\s)]+/[^/\s)]+/(?:issues|pull)/\d+")


def _markdown(root: Path) -> list[Path]:
    """Every markdown file under `root`, in a stable order."""
    return sorted(root.rglob("*.md"))


def _ids(paths: list[Path]) -> list[str]:
    """Repo-relative POSIX names, so a failure names the document rather than an index."""
    return [path.relative_to(_ROOT).as_posix() for path in paths]


def _relative_links(text: str) -> list[str]:
    """The inline link targets that have to resolve on disk — no schemes, no bare anchors."""
    targets = []
    for target in _LINK.findall(text):
        if target.startswith(_ABSOLUTE) or target.startswith("#"):
            continue
        targets.append(target)
    return targets


def _status_table(text: str) -> list[str]:
    """The table rows under a document's `## Status` heading, `|` lines and nothing else."""
    lines = text.split("\n")
    heading = next((i for i, line in enumerate(lines) if line.strip() == "## Status"), None)
    if heading is None:
        return []
    rows = []
    for line in lines[heading + 1 :]:
        if line.startswith("|"):
            rows.append(line)
        elif rows and line.startswith("#"):
            break
    return rows


def _cells(row: str) -> list[str]:
    """One table row's cells, stripped, without the leading and trailing pipe."""
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _data_rows(rows: list[str]) -> list[str]:
    """The rows carrying a decision — the header and the `|---|` separator dropped.

    An empty tracking cell must reach the caller, so a row is dropped for being the separator
    rather than for holding nothing.
    """
    return [
        row for row in rows[1:] if not all(re.fullmatch(r":?-+:?", cell) for cell in _cells(row))
    ]


_ALL_DOCS = _markdown(_DOCS)
_MAIN_DOCS = [
    path
    for path in _markdown(_SANDBOX)
    if path != _SANDBOX / "README.md" and _RESEARCH not in path.parents
]
_RECORDS = _markdown(_RESEARCH)
_INDEX_READMES = [_SANDBOX / "kinds" / "README.md", _SANDBOX / "backends" / "README.md"]


class TestEveryRelativeLinkResolves:
    """A link into the tree points at something that is there."""

    @pytest.mark.parametrize("doc", _ALL_DOCS, ids=_ids(_ALL_DOCS))
    def test_the_targets_exist(self, doc: Path):
        broken = []
        for target in _relative_links(doc.read_text("utf-8")):
            resolved = (doc.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                broken.append(f"{doc.relative_to(_ROOT).as_posix()} -> {target} ({resolved})")
        assert not broken, "unresolved links:\n" + "\n".join(broken)


class TestEveryMainDocumentEndsWithATracker:
    """The main documents are written in the present tense, and the tracker is what pays for it."""

    @pytest.mark.parametrize("doc", _MAIN_DOCS, ids=_ids(_MAIN_DOCS))
    def test_it_has_a_status_table(self, doc: Path):
        text = doc.read_text("utf-8")
        assert "## Status" in text, f"{doc.relative_to(_ROOT).as_posix()} has no `## Status`"
        assert _status_table(text), (
            f"{doc.relative_to(_ROOT).as_posix()} has a `## Status` heading with no table under it"
        )

    def test_the_front_door_and_the_records_are_exempt(self):
        assert _SANDBOX / "README.md" not in _MAIN_DOCS
        assert not [path for path in _MAIN_DOCS if _RESEARCH in path.parents]


class TestEveryTrackerRowIsPinned:
    """No empty tracking cell — a GitHub link, a `.md`, the word `untracked`, or an em dash."""

    @pytest.mark.parametrize("doc", _MAIN_DOCS, ids=_ids(_MAIN_DOCS))
    def test_the_last_cell_says_something(self, doc: Path):
        unpinned = []
        for row in _data_rows(_status_table(doc.read_text("utf-8"))):
            tracking = _cells(row)[-1]
            pinned = (
                re.search(r"\(https://github\.com/[^)]+\)", tracking) is not None
                or "untracked" in tracking
                or "—" in tracking
            )
            if not pinned and not re.search(r"\]\([^)]+\.md[^)]*\)", tracking):
                unpinned.append(f"{doc.relative_to(_ROOT).as_posix()}: {row.strip()}")
        assert not unpinned, "tracking cells that pin nothing:\n" + "\n".join(unpinned)


class TestEveryResearchRecordOpensWithABanner:
    """A record says what it is and where the decided content went, before anything else."""

    @pytest.mark.parametrize("doc", _RECORDS, ids=_ids(_RECORDS))
    def test_a_blockquote_starts_within_the_first_five_lines(self, doc: Path):
        head = doc.read_text("utf-8").split("\n")[:5]
        assert any(line.startswith(">") for line in head), (
            f"{doc.relative_to(_ROOT).as_posix()} opens without a record banner"
        )

    @pytest.mark.parametrize("doc", _MAIN_DOCS, ids=_ids(_MAIN_DOCS))
    def test_no_main_document_carries_the_old_banner_grammar(self, doc: Path):
        text = doc.read_text("utf-8")
        found = [banner for banner in _OLD_BANNERS if banner in text]
        assert not found, (
            f"{doc.relative_to(_ROOT).as_posix()} carries {found} — "
            "location conveys status now, and the `## Status` table carries the detail"
        )


class TestNoRecordCitesALine:
    """A record is never edited to match what shipped, so a line number in one cannot be repaired.

    `check_doc_paths.py` holds a line reference to the definition it names, which is a promise
    about *today's* source. A record makes no such promise — it is kept in the tense it was
    written — so a number in one rots by construction and is left pointing into the middle of
    whatever moved. Its parser is reused here rather than approximated, since a second, weaker
    reading of what a reference looks like would pass exactly the ones it failed to recognise.
    """

    @pytest.mark.parametrize("doc", _markdown(_RESEARCH), ids=_ids(_markdown(_RESEARCH)))
    def test_it_names_no_line(self, doc: Path):
        cited = _check.line_references(_check.document_text(doc))
        assert [reference.written for reference in cited] == []


class TestTheIndexReadmesAreSelfSufficient:
    """A group's index README pins through the page that owns the subject, never an issue.

    The per-item pages and the sibling main documents carry the issue trail; the index says what
    is true and links to whoever tracks it. A reader who cannot reach GitHub loses nothing, and a
    row cannot go stale against a tracker the README never named.
    """

    @pytest.mark.parametrize("doc", _INDEX_READMES, ids=_ids(_INDEX_READMES))
    def test_it_links_no_issue_or_pull_request(self, doc: Path):
        found = _ISSUE_LINK.findall(doc.read_text("utf-8"))
        assert not found, (
            f"{doc.relative_to(_ROOT).as_posix()} links "
            + ", ".join(found)
            + " — an index README pins through the owning page, so move the reference into "
            "that page and link the `.md` instead"
        )
