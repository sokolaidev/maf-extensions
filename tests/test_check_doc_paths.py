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
            "samples/01_acas_bicep/main.bicep",
            "docs/sandbox/config.yaml",
            "packages/p/pyproject.toml",
            "docs/a.markdown",
        ],
    )
    def test_an_extension_of_any_length_is_still_a_path(self, repo, named: str):
        root = repo({"README.md": f"see {named}"})
        assert check.broken_prose_paths(root) == [f"README.md: names -> {named}"]

    def test_a_version_is_not_read_as_a_path(self, repo):
        """The extension must start with a letter, or `maf-sandbox/0.19.0` becomes a file."""
        root = repo({"README.md": "pin packages/maf-sandbox/0.19.0 exactly"})
        assert check.broken_prose_paths(root) == []

    def test_a_nested_link_label_does_not_hide_the_target(self, repo):
        """A label may hold balanced brackets, so `[see [details]](x)` is a link.

        Stopping at the first `]` finds no `(` after it, reads the whole construct as prose,
        and lets the destination through unchecked — dead or alive, which is the direction
        that matters.
        """
        root = repo({"a.md": "[see [details]](docs/gone.md)"})
        assert check.broken_links(root) == ["a.md: link -> docs/gone.md"]

    def test_a_link_wrapping_an_image_names_two_files_and_both_are_checked(self, repo):
        """Every badge in this repository is `[![alt](image)](link)`.

        Reading only the outer destination drops the image; reading only the inner one drops
        the link. Either way a moved file passes, and the gate stays green over the miss.
        """
        root = repo({"a.md": "[![alt](img/gone.png)](docs/gone.md)"})
        assert check.broken_links(root) == [
            "a.md: link -> docs/gone.md",
            "a.md: link -> img/gone.png",
        ]

    def test_an_escaped_backslash_does_not_escape_the_link_after_it(self, repo):
        """`\\\\[x](gone.md)` renders a literal backslash and then a live link.

        Escaping is the parity of the backslash run, not the one character in front. Reading
        an even run as an escape skips a real link and its target is never resolved.
        """
        root = repo({"a.md": "\\\\[x](docs/gone.md)\n"})
        assert check.broken_links(root) == ["a.md: link -> docs/gone.md"]

    def test_a_heading_inside_an_html_comment_plants_no_anchor(self, repo):
        """A commented-out section is exactly how an anchor stops existing, so a link to one
        must be reported — reading the comment as markdown invents the anchor instead."""
        root = repo({"a.md": "<!--\n## Gone\n-->\n\n[x](#gone)\n"})
        assert check.broken_links(root) == ["a.md: heading -> #gone"]


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
        """A package-relative reference is dead when only some other package has that file."""
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

    def test_a_fragment_on_a_source_file_is_not_a_heading(self, repo):
        """A non-markdown target has no headings, so every fragment on one is left alone —
        `#L42`, and anything else. It is the suffix guard doing this, not the line-reference
        rule, which is why the two are tested apart."""
        root = repo({"a.md": "[x](s/m.py#L42) [y](s/m.py#anything)", "s/m.py": "x = 1\n"})
        assert check.broken_links(root) == []

    def test_a_protocol_relative_url_is_external(self, repo):
        """No scheme to match on, and no more ours for it — resolving one as a path reports a
        working external link as a missing file."""
        root = repo({"README.md": "[x](//example.invalid/docs) [y](//cdn.example.invalid/a.png)"})
        assert check.broken_links(root) == []

    def test_a_link_inside_a_fenced_sample_is_not_checked(self, repo):
        """A document showing its reader what a link looks like is not making one."""
        root = repo({"a.md": "Example:\n\n```markdown\n[example](missing.md)\n```\n"})
        assert check.broken_links(root) == []

    def test_a_link_inside_a_code_span_is_not_checked(self, repo):
        """The inline half of the same rule: a code span holds characters, not markup."""
        root = repo({"a.md": "Write `[example](missing.md)` to link.\n"})
        assert check.broken_links(root) == []

    def test_an_unclosed_backtick_does_not_blank_the_rest_of_the_page(self, repo):
        """A run with no closer is literal text, so the link after it is still a link."""
        root = repo({"a.md": "A stray ` tick, then [x](missing.md).\n"})
        assert check.broken_links(root) == ["a.md: link -> missing.md"]

    def test_a_destination_in_angle_brackets_may_hold_a_space(self, repo):
        """`[x](<a file.md>)` is one target; reading it to the first space invents `a`."""
        root = repo({"a.md": "[x](<docs/a file.md>)", "docs/a file.md": "y"})
        assert check.broken_links(root) == []

    def test_a_percent_encoded_destination_names_the_tracked_file(self, repo):
        """A destination is a URL even when relative, so `%20` is the space in the filename."""
        root = repo({"a.md": "[x](docs/a%20file.md)", "docs/a file.md": "y"})
        assert check.broken_links(root) == []

    def test_a_link_inside_an_html_comment_is_not_checked(self, repo):
        """A comment holds what its author stopped publishing; GitHub renders none of it."""
        root = repo({"a.md": "<!-- [x](missing.md) -->\n"})
        assert check.broken_links(root) == []

    def test_an_unterminated_destination_is_not_a_link(self, repo):
        """Nothing closes the `(`, so GitHub renders `[x](missing.md` as those characters.

        Resolving what follows the paren reports prose that points nowhere, and the text most
        likely to hit it is a document explaining the syntax.
        """
        root = repo({"a.md": "The form is [x](missing.md and then nothing.\n"})
        assert check.broken_links(root) == []

    def test_a_repo_shaped_path_in_a_url_query_is_not_a_local_reference(self, repo):
        """`?file=docs/gone.md` puts a repo-shaped path after `=`, where a bare mention never
        sits — and the character in front is the only thing a lookbehind can judge."""
        root = repo({"a.md": "See https://example.invalid/view?file=docs/gone.md for it.\n"})
        assert check.broken_prose_paths(root) == []

    def test_a_protocol_relative_url_in_prose_is_external_too(self, repo):
        """`is_local` already refuses `//host/x` for a destination; prose gets the same rule,
        or a query string on one is read as a local path that names nothing."""
        root = repo({"a.md": "See //example.invalid/view?file=docs/gone.md for it.\n"})
        assert check.broken_prose_paths(root) == []

    def test_a_balanced_parenthesis_belongs_to_the_destination(self, repo):
        """Truncating `docs/a_(draft).md` at the first `)` reports a tracked file as missing."""
        root = repo({"a.md": "[x](docs/a_(draft).md)", "docs/a_(draft).md": "y"})
        assert check.broken_links(root) == []

    def test_a_test_naming_a_path_that_must_not_exist_is_out_of_scope(self, repo):
        """`tests/` constructs paths as data, including ones that are absent on purpose."""
        root = repo({"tests/test_x.py": 'missing = "docs/design/nowhere.md"\n'})
        assert check.broken_prose_paths(root) == []


class TestLinkSyntax:
    """Both halves of `[label](destination)` nest, and each nests in its own way.

    These pin the extraction directly rather than through `broken_links`, because a scanner
    that quietly finds *fewer* links leaves the gate green while checking less than it did.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[x](docs/a.md)", ["docs/a.md"]),
            ("![alt](img/a.png)", ["img/a.png"]),
            ("[x](<docs/a file.md>)", ["docs/a file.md"]),
            ('[x](docs/a.md "Title")', ["docs/a.md"]),
            ("[a](one.md) and [b](two.md)", ["one.md", "two.md"]),
            ("[x](docs/a_(draft).md)", ["docs/a_(draft).md"]),
            ("[see [details]](docs/a.md)", ["docs/a.md"]),
            ("[![alt](i.png)](docs/a.md)", ["docs/a.md", "i.png"]),
            # A bracketed sentence is not a link, but the link inside it still is.
            ("[see [x](a.md)]", ["a.md"]),
            ("[x](#section)", ["#section"]),
            ("[x](docs/a.md 'Title')", ["docs/a.md"]),
            ("[x](docs/a.md (Title))", ["docs/a.md"]),
            ("[x](\n  docs/a.md\n)", ["docs/a.md"]),
            ("\\[not a link](a.md)", []),
            # An even run of backslashes is escaped backslashes, and the `[` stays active:
            # `\\[x](a.md)` renders a literal backslash *followed by a live link*.
            ("\\\\[x](a.md)", ["a.md"]),
            ("\\\\\\[x](a.md)", []),
            # Nothing closes the `(`, so GitHub renders the characters and there is no link.
            ("[x](missing.md", []),
            ("[x](<unclosed.md", []),
            ('[x](a.md "unclosed', []),
            ("[dangling (a.md)", []),
            ("[x]()", []),
            ("Use [label] here.", []),
        ],
    )
    def test_an_inline_target_is_read_whole(self, text: str, expected: list[str]):
        assert check.inline_link_targets(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[label]: docs/a.md\n", ["docs/a.md"]),
            ("[label]: <docs/a file.md>\n", ["docs/a file.md"]),
            ('[label]: docs/a.md "Title"\n', ["docs/a.md"]),
            ("[see [details]]: docs/a.md\n", ["docs/a.md"]),
            ("   [label]: docs/a.md\n", ["docs/a.md"]),
            # Four spaces is an indented code block, and a space before the colon is prose.
            ("     [label]: docs/a.md\n", []),
            ("  [label] : docs/a.md\n", []),
        ],
    )
    def test_a_reference_definition_is_read_the_same_way(self, text: str, expected: list[str]):
        assert check.reference_link_targets(text) == expected


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

    def test_a_literal_numbered_heading_does_not_collide_with_a_generated_one(self):
        """`## Notes`, `## Notes`, `## Notes-1` is three anchors on GitHub, not two."""
        found = check.anchors("## Notes\n\n## Notes\n\n## Notes-1\n")
        assert found == {"notes", "notes-1", "notes-1-1"}

    def test_a_longer_fence_is_not_closed_by_a_shorter_one(self, repo):
        """A run shorter than the opener is content, so the block runs on past it."""
        nested = "````\n# Not a heading\n```\n# Also not one\n````\n"
        assert check.anchors(nested) == set()

    def test_a_closing_fence_carries_no_info_string(self, repo):
        """CommonMark gives an info string to the opener only, so ```python inside an open
        block is a line of the sample. Ending the block there hands the rest of it to the
        heading reader as markdown."""
        nested = "```\n# Not a heading\n```python\n# Also not one\n```\n"
        assert check.anchors(nested) == set()

    def test_a_line_reference_on_a_markdown_target_is_dead_unless_it_asks_for_the_plain_view(
        self, repo
    ):
        """A rendered markdown page has no `L42` anchor, so the fragment goes nowhere.

        `?plain=1` is the one exception: it asks for the line-numbered source listing. Any
        other query still renders markdown, so exempting on the presence of a query rather
        than on its content accepts a dead link.
        """
        root = repo({"a.md": "[x](b.md#L42)", "b.md": "## Something\n"})
        assert check.broken_links(root) == ["a.md: heading -> b.md#L42"]

        plain = repo({"a.md": "[x](b.md?plain=1#L42)", "b.md": "## Something\n"})
        assert check.broken_links(plain) == []

        other = repo({"a.md": "[x](b.md?rev=2#L42)", "b.md": "## Something\n"})
        assert check.broken_links(other) == ["a.md: heading -> b.md?rev=2#L42"]

    def test_a_heading_inside_a_blockquote_is_a_heading(self, repo):
        """`> ### Title` renders as a heading with an anchor, and this repository has two.

        The markers are not indentation, so counting them against the three spaces an ATX
        heading may carry rejects a link that works.
        """
        root = repo(
            {
                "a.md": "[x](b.md#refuse-never-degrade) [y](b.md#deeper) [z](b.md#quoted-setext)",
                "b.md": "> ### Refuse, never degrade.\n\n> > ## Deeper\n\n> Quoted setext\n> ---\n",
            }
        )
        assert check.broken_links(root) == []

    def test_an_underline_after_a_heading_is_not_a_second_heading(self, repo):
        """`===` under `# Foo` is a paragraph — GitHub renders one heading there, not two."""
        assert check.headings("# Foo\n===\n") == ["Foo"]
        assert check.anchors("# Foo\n===\n") == {"foo"}

    def test_a_setext_heading_is_a_heading(self, repo):
        """GitHub renders text underlined by `=` or `-` as a heading and slugs it the same."""
        root = repo(
            {"a.md": "[x](b.md#title) [y](b.md#second)", "b.md": "Title\n=====\n\nSecond\n---\n"}
        )
        assert check.broken_links(root) == []

    def test_headings_are_numbered_across_both_spellings_in_document_order(self):
        """The numbering is positional, so a setext heading between two ATX ones is second."""
        assert check.headings("# One\n\nTwo\n===\n\n## Three\n") == ["One", "Two", "Three"]
        assert check.anchors("# Notes\n\nNotes\n=====\n") == {"notes", "notes-1"}

    def test_a_code_span_in_a_heading_contributes_its_text(self, repo):
        """Stripping code spans before reading headings would change the slug GitHub derives."""
        root = repo(
            {"a.md": "[x](b.md#the-run_code-contract)", "b.md": "## The `run_code` contract\n"}
        )
        assert check.broken_links(root) == []

    def test_an_anchor_inside_an_html_comment_plants_nothing(self, repo):
        """GitHub renders a comment as nothing, so the markup inside one reaches no reader."""
        root = repo({"a.md": '<!-- <a id="gone"></a> -->\n\n[x](#gone)\n'})
        assert check.broken_links(root) == ["a.md: heading -> #gone"]

    def test_an_anchor_inside_a_code_span_plants_nothing(self, repo):
        """A document showing the markup for an anchor is not planting one."""
        root = repo({"a.md": 'Write `<a id="gone"></a>` to plant one.\n\n[x](#gone)\n'})
        assert check.broken_links(root) == ["a.md: heading -> #gone"]

    def test_a_percent_encoded_fragment_matches_the_heading_it_names(self, repo):
        """A fragment travels URL-encoded, and GitHub slugged the heading decoded."""
        root = repo({"a.md": "[x](b.md#caf%C3%A9)", "b.md": "## Café\n"})
        assert check.broken_links(root) == []

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
