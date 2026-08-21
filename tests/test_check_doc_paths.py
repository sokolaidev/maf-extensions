"""The check that reads a documentation reference and says whether it resolves.

`scripts/check_doc_paths.py` exists because a restructure moved `docs/design/` to
`docs/sandbox/`, left eleven references behind, and went green (#556). So the test that matters
most is the one proving it **fails** on that shape: a checker written against the same
blind spot it is meant to close passes everything.

The second half is quietness. Its first run over this repository reported eleven references
that were all correct — prose inside a package naming that package's own `tests/`, and a
script naming an artefact a run produces. A checker nobody trusts gets switched off, and a
switched-off checker finds nothing, so those cases are pinned here too.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_doc_paths", _ROOT / "scripts" / "check_doc_paths.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


@pytest.fixture
def repo(tmp_path: Path):
    """A real git repository, because the script reads `git ls-files` rather than globbing.

    Tracked-only is the point: a scratch file a contributor left in the tree is not
    documentation, and scanning it would report a reference nobody published.
    """

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
    """A reference to a document that moved. Both spellings, because both were left behind."""

    def test_a_markdown_link_to_a_moved_document_is_reported(self, repo):
        root = repo({"docs/a.md": "see [it](../docs/design/gone.md)", "docs/sandbox/gone.md": "x"})
        assert check.broken_links(root) == ["docs/a.md: link -> ../docs/design/gone.md"]

    def test_a_prose_path_in_shipped_source_is_reported(self, repo):
        root = repo(
            {
                "packages/p/src/m/_x.py": '"""See ``docs/design/gone.md`` for why."""\n',
                "docs/sandbox/gone.md": "x",
            }
        )
        assert check.broken_prose_paths(root) == [
            "packages/p/src/m/_x.py: names -> docs/design/gone.md"
        ]

    def test_a_moved_document_is_not_rescued_by_the_suffix_rule(self, repo):
        """The rule that keeps this quiet must not swallow the defect. A move changes a
        *middle* segment, so the old path is a suffix of nothing — this is what makes the
        leniency below safe rather than merely convenient."""
        root = repo(
            {"docs/sandbox/architecture.md": "x", "README.md": "see docs/design/architecture.md"}
        )
        assert check.broken_prose_paths(root) == ["README.md: names -> docs/design/architecture.md"]


class TestWhatItMustNotReport:
    """Every case here was a real finding on its first run, and every one of them was correct."""

    def test_a_package_relative_path_resolves_against_the_package(self, repo):
        """Prose inside a package saying `tests/test_x.py` means that package's tests."""
        root = repo(
            {
                "packages/p/README.md": "see tests/test_x.py",
                "packages/p/tests/test_x.py": "x",
            }
        )
        assert check.broken_prose_paths(root) == []

    def test_a_design_document_may_name_a_packages_own_file(self, repo):
        """From `docs/`, `scripts/import_disk_image.py` means whichever package ships it."""
        root = repo(
            {
                "docs/d.md": "run scripts/import_disk_image.py",
                "packages/p/scripts/import_disk_image.py": "x",
            }
        )
        assert check.broken_prose_paths(root) == []

    def test_a_glob_is_a_pattern_and_not_a_path(self, repo):
        root = repo({"README.md": "linted: packages/*/README.md and samples/**/*.md"})
        assert check.broken_prose_paths(root) == []

    def test_a_url_containing_a_repo_shaped_path_is_not_a_local_reference(self, repo):
        root = repo({"README.md": "https://github.com/o/r/blob/main/packages/p/src/gone.py"})
        assert check.broken_prose_paths(root) == []

    def test_an_external_link_is_never_resolved(self, repo):
        """Reaching the network here would make the gate slow and flaky, and a dead external
        link is a different problem on a different cadence."""
        root = repo(
            {"README.md": "[x](https://example.invalid/gone) [y](mailto:a@example.invalid)"}
        )
        assert check.broken_links(root) == []

    def test_a_bare_fragment_points_within_the_page(self, repo):
        root = repo({"README.md": "[x](#a-heading)"})
        assert check.broken_links(root) == []

    def test_an_anchor_is_stripped_before_the_file_is_resolved(self, repo):
        """File existence only. Checking the heading too needs a markdown parse, and the
        reference still resolves to a real document without it."""
        root = repo({"a.md": "[x](b.md#section-that-is-not-checked)", "b.md": "x"})
        assert check.broken_links(root) == []

    def test_an_untracked_file_is_not_scanned(self, repo):
        root = repo({"README.md": "fine"})
        (root / "scratch.md").write_text("see docs/design/gone.md", encoding="utf-8")
        assert check.broken_prose_paths(root) == []

    def test_a_test_naming_a_path_that_must_not_exist_is_out_of_scope(self, repo):
        """`tests/` constructs paths as data, including ones that are absent on purpose."""
        root = repo({"tests/test_x.py": 'missing = "docs/design/nowhere.md"\n'})
        assert check.broken_prose_paths(root) == []


class TestTheVerdict:
    def test_a_clean_tree_exits_zero(self, capsys, monkeypatch, repo):
        root = repo({"README.md": "[x](docs/a.md)", "docs/a.md": "y"})
        monkeypatch.setattr(check, "repo_root", lambda: root)
        assert check.main(["check_doc_paths.py"]) == 0
        assert "resolves" in capsys.readouterr().out

    def test_a_dangling_reference_exits_one_and_names_it(self, capsys, monkeypatch, repo):
        root = repo({"README.md": "see docs/design/gone.md", "docs/sandbox/gone.md": "y"})
        monkeypatch.setattr(check, "repo_root", lambda: root)
        assert check.main(["check_doc_paths.py"]) == 1
        assert "docs/design/gone.md" in capsys.readouterr().err

    def test_an_argument_is_refused(self, capsys):
        assert check.main(["check_doc_paths.py", "extra"]) == 2


class TestThisRepository:
    """The check against the tree it ships in — the one that keeps it honest as things move."""

    def test_every_reference_in_this_repository_resolves(self):
        problems = check.broken_links(_ROOT) + check.broken_prose_paths(_ROOT)
        assert problems == []
