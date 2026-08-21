"""What `scripts/check_doc_paths.py` accepts, and what it must never accept.

Three references are checked — a relative link, the heading fragment on one, and a repository
path in prose — and the suite is written around the two ways each can be wrong. Reporting a
working reference gets the check switched off; passing a dead one is the defect it exists for.

The boundaries that carry the most weight: resolution is against the *tracked* tree, so an
untracked file satisfies nothing; a path written inside a package resolves against that package
and no other; and a fenced block is not markdown, so an ATX-looking line in one is not a
heading.
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
    documentation, and resolving against one would pass a reference nobody else can follow.
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
    """A reference to a document that moved, in both spellings."""

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

    @pytest.mark.parametrize(
        "named",
        [
            # Every one of these was invisible while the extension was capped at four
            # characters, which is the length of the paths most likely to move.
            "samples/01_acas_bicep/main.bicep",
            "docs/sandbox/config.yaml",
            "packages/p/pyproject.toml",
            "docs/a.markdown",
        ],
    )
    def test_an_extension_longer_than_four_characters_is_still_a_path(self, repo, named: str):
        root = repo({"README.md": f"see {named}"})
        assert check.broken_prose_paths(root) == [f"README.md: names -> {named}"]

    def test_a_version_is_not_read_as_a_path(self, repo):
        """The extension must start with a letter, or `maf-sandbox/0.19.0` becomes a file."""
        root = repo({"README.md": "pin packages/maf-sandbox/0.19.0 exactly"})
        assert check.broken_prose_paths(root) == []


class TestResolutionIsAgainstTheTrackedTree:
    """An untracked file satisfies nothing: a reference only works for someone who cloned."""

    def test_an_untracked_file_does_not_satisfy_a_prose_path(self, repo):
        root = repo({"README.md": "see docs/local.md"})
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "local.md").write_text("built locally", encoding="utf-8")
        assert check.broken_prose_paths(root) == ["README.md: names -> docs/local.md"]

    def test_an_untracked_file_does_not_satisfy_a_link(self, repo):
        root = repo({"a.md": "[x](out/report.md)"})
        (root / "out").mkdir(exist_ok=True)
        (root / "out" / "report.md").write_text("an artefact", encoding="utf-8")
        assert check.broken_links(root) == ["a.md: link -> out/report.md"]

    def test_a_link_to_a_directory_holding_a_tracked_file_resolves(self, repo):
        """Git tracks no directory, and this repository's README alone links seven of them."""
        root = repo({"a.md": "[x](pkg/) and [y](pkg)", "pkg/thing.py": "x = 1\n"})
        assert check.broken_links(root) == []

    def test_a_link_that_leaves_the_repository_is_reported(self, repo):
        root = repo({"docs/a.md": "[x](../../elsewhere/thing.md)"})
        assert check.broken_links(root) == ["docs/a.md: link -> ../../elsewhere/thing.md"]

    def test_an_untracked_file_is_not_scanned(self, repo):
        root = repo({"README.md": "fine"})
        (root / "scratch.md").write_text("see docs/design/gone.md", encoding="utf-8")
        assert check.broken_prose_paths(root) == []


class TestAPathInsideAPackageMeansThatPackage:
    """The leniency that keeps this quiet must not let one package satisfy another's reference."""

    def test_a_package_relative_path_resolves_against_its_own_package(self, repo):
        root = repo(
            {"packages/p/README.md": "see tests/test_x.py", "packages/p/tests/test_x.py": "x"}
        )
        assert check.broken_prose_paths(root) == []

    def test_another_packages_file_does_not_satisfy_it(self, repo):
        """`packages/p` naming `tests/test_x.py` when only `packages/q` has one is a dead
        reference, and a repository-wide search reported it as live."""
        root = repo(
            {"packages/p/README.md": "see tests/test_x.py", "packages/q/tests/test_x.py": "x"}
        )
        assert check.broken_prose_paths(root) == ["packages/p/README.md: names -> tests/test_x.py"]

    def test_a_document_outside_every_package_may_name_a_packages_file(self, repo):
        """From `docs/`, `scripts/import_disk_image.py` means whichever package ships it."""
        root = repo(
            {
                "docs/d.md": "run scripts/import_disk_image.py",
                "packages/p/scripts/import_disk_image.py": "x",
            }
        )
        assert check.broken_prose_paths(root) == []

    def test_a_full_path_from_the_root_resolves_from_anywhere(self, repo):
        root = repo(
            {
                "packages/p/README.md": "see packages/q/tests/test_x.py",
                "packages/q/tests/test_x.py": "x",
            }
        )
        assert check.broken_prose_paths(root) == []


class TestWhatItMustNotReport:
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

    def test_a_heading_that_exists_resolves(self, repo):
        root = repo({"a.md": "[x](b.md#the-section)", "b.md": "## The section\n"})
        assert check.broken_links(root) == []

    def test_a_line_reference_on_a_source_file_is_not_a_heading(self, repo):
        """`#L42` is what GitHub renders for a line, and no heading would ever match it."""
        root = repo({"a.md": "[x](s/m.py#L42) [y](s/m.py#L42-L60)", "s/m.py": "x = 1\n"})
        assert check.broken_links(root) == []

    def test_a_test_naming_a_path_that_must_not_exist_is_out_of_scope(self, repo):
        """`tests/` constructs paths as data, including ones that are absent on purpose."""
        root = repo({"tests/test_x.py": 'missing = "docs/design/nowhere.md"\n'})
        assert check.broken_prose_paths(root) == []


class TestAnchors:
    """A renamed section breaks a link the way a moved file does, and nothing else moves."""

    def test_a_heading_that_does_not_exist_is_reported(self, repo):
        root = repo({"a.md": "[x](b.md#the-old-name)", "b.md": "## The new name\n"})
        assert check.broken_links(root) == ["a.md: heading -> b.md#the-old-name"]

    def test_a_fragment_pointing_within_the_same_page_is_checked_too(self, repo):
        root = repo({"a.md": "## Here\n\n[up](#gone) and [back](#here)\n"})
        assert check.broken_links(root) == ["a.md: heading -> #gone"]

    def test_a_comment_in_a_fenced_block_is_not_a_heading(self, repo):
        """A fence holds some other language, so `#` starts a comment there. Reading them as
        headings invents anchors no rendered page has, and a link to one then passes."""
        fenced = "```python\n# On the backend, read with getattr\nx = 1\n```\n"
        root = repo({"a.md": "[x](b.md#on-the-backend-read-with-getattr)", "b.md": fenced})
        assert check.broken_links(root) == [
            "a.md: heading -> b.md#on-the-backend-read-with-getattr"
        ]

    def test_a_tilde_fence_is_closed_by_tildes_and_not_by_backticks(self, repo):
        root = repo({"a.md": "[x](b.md#inside)", "b.md": "~~~\n# Inside\n~~~\n"})
        assert check.broken_links(root) == ["a.md: heading -> b.md#inside"]

    def test_a_heading_after_a_fenced_block_is_still_a_heading(self, repo):
        root = repo({"a.md": "[x](b.md#after)", "b.md": "```\n# not one\n```\n\n## After\n"})
        assert check.broken_links(root) == []

    @pytest.mark.parametrize(
        ("heading", "slug"),
        [
            ("Plain words", "plain-words"),
            ("The `SandboxSpec` field", "the-sandboxspec-field"),
            ("Upgrading to 0.20", "upgrading-to-020"),
            ("**Bold** and *italic*", "bold-and-italic"),
            ("[A link](https://example.invalid)", "a-link"),
            # Each whitespace character becomes its own hyphen, so a stripped em dash between
            # two spaces leaves two. Collapsing them reports a working link as broken.
            ("Host tools — the contract", "host-tools--the-contract"),
            # `_` is a word character GitHub keeps, and every identifier here has one.
            ("Reaching it via `dispatch_over_exec`", "reaching-it-via-dispatch_over_exec"),
        ],
    )
    def test_the_slug_matches_what_github_derives(self, heading: str, slug: str):
        assert check.slugify(heading) == slug

    def test_a_repeated_heading_is_numbered_from_the_second(self):
        assert check.anchors("## Notes\n\ntext\n\n## Notes\n\nmore\n") == {"notes", "notes-1"}

    def test_an_explicit_html_anchor_counts(self, repo):
        root = repo({"a.md": "[x](b.md#planted)", "b.md": '<a id="planted"></a>\n\n# Title\n'})
        assert check.broken_links(root) == []

    def test_a_closing_hash_run_is_not_part_of_the_heading(self, repo):
        root = repo({"a.md": "[x](b.md#the-section)", "b.md": "## The section ##\n"})
        assert check.broken_links(root) == []


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
        assert check.broken_links(_ROOT) + check.broken_prose_paths(_ROOT) == []
