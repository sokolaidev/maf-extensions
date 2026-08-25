"""Regression tests for the PR title versus diff semantic guard."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_title_diff.py"
_spec = importlib.util.spec_from_file_location("check_title_diff", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


_DOC_BEFORE = '"""old documentation"""\ndef run(value: int) -> int:\n    """old function docs"""\n    return value\n'
_DOC_AFTER = '"""new documentation"""\ndef run(value: int) -> int:\n    """new function docs"""\n    return value\n'


class TestNormalizedPython:
    def test_module_and_function_docstrings_do_not_count(self):
        assert not check.python_changed(_DOC_BEFORE, _DOC_AFTER)

    def test_a_changed_default_counts(self):
        before = "def run(value: int = 1) -> int:\n    return value\n"
        after = "def run(value: int = 2) -> int:\n    return value\n"
        assert check.python_changed(before, after)

    def test_a_comment_and_formatting_change_does_not_count(self):
        before = "def run(value: int) -> int:\n    return value\n"
        after = "# explanation\ndef run(value: int) -> int:\n\n    return value\n"
        assert not check.python_changed(before, after)


class TestAssess:
    def test_fix_on_documentation_only_python_is_rejected(self):
        problems = check.assess(
            "fix: clarify the API",
            ["packages/maf-sandbox/src/maf_sandbox/_protocol.py"],
            {"packages/maf-sandbox/src/maf_sandbox/_protocol.py": (_DOC_BEFORE, _DOC_AFTER)},
        )
        assert problems
        assert "no executable change" in problems[0]

    @pytest.mark.parametrize("kind", ["docs", "chore", "refactor", "test", "build", "ci"])
    def test_non_behavior_title_on_executable_python_is_rejected(self, kind: str):
        before = "def run() -> int:\n    return 1\n"
        after = "def run() -> int:\n    return 2\n"
        problems = check.assess(
            f"{kind}: update implementation",
            ["packages/example/src/example.py"],
            {"packages/example/src/example.py": (before, after)},
        )
        assert problems
        assert "non-behavioral" in problems[0]
        assert "valid title prefixes are:" in problems[1]
        for title_type in check._VALID_TYPES:
            assert f"{title_type}:" in problems[1]
            assert f"{title_type}(...):" in problems[1]
            assert f"{title_type}!:" in problems[1]
            assert f"{title_type}(...)!:" in problems[1]
        assert "use a behavior type" in problems[1]
        assert "separate pull request" in problems[1]

    def test_chore_title_on_changed_tests_is_allowed(self):
        before = "def run() -> int:\n    return 1\n"
        after = "def run() -> int:\n    return 2\n"
        assert (
            check.assess(
                "chore: refresh test coverage",
                ["packages/maf-sandbox/tests/test_router.py"],
                {"packages/maf-sandbox/tests/test_router.py": (before, after)},
            )
            == []
        )

    def test_behavior_title_on_changed_tests_is_rejected(self):
        before = "def run() -> int:\n    return 1\n"
        after = "def run() -> int:\n    return 2\n"
        assert check.assess(
            "fix: correct test expectation",
            ["tests/test_router.py"],
            {"tests/test_router.py": (before, after)},
        )

    @pytest.mark.parametrize("path", ["requirements.txt", "constraints.txt"])
    def test_dependency_text_is_executable_metadata(self, path: str):
        assert check.assess("chore: update dependencies", [f"packages/example/{path}"], {})

    def test_docs_title_on_markdown_is_allowed(self):
        assert check.assess("docs: explain the API", ["README.md"], {}) == []

    def test_behavior_title_diagnostic_is_not_documentation_only_for_tests(self):
        before = "def run() -> int:\n    return 1\n"
        after = "def run() -> int:\n    return 2\n"
        problems = check.assess(
            "fix: update test",
            ["tests/test_router.py"],
            {"tests/test_router.py": (before, after)},
        )
        assert "no executable change" in problems[0]
        assert "documentation-only" not in problems[0]

    def test_behavior_title_requires_executable_changes_in_each_package(self):
        before = "def run() -> int:\n    return 1\n"
        after = "def run() -> int:\n    return 2\n"
        assert check.assess(
            "feat: update both packages",
            ["packages/a/src/a.py", "packages/b/README.md"],
            {"packages/a/src/a.py": (before, after)},
            [("packages/a/src/a.py",), ("packages/b/README.md",)],
        )

    def test_ci_title_on_global_executable_file_is_allowed(self):
        assert check.assess("ci: update workflow", [".github/workflows/ci.yml"], {}) == []

    def test_ci_title_on_this_repository_change_is_allowed(self):
        paths = [
            ".github/workflows/pr-title.yml",
            "AGENTS.md",
            "CONTRIBUTING.md",
            "scripts/check_title_diff.py",
            "tests/test_check_title_diff.py",
        ]
        assert check.assess("ci: enforce title policy", paths, {}) == []

    def test_a_module_moved_into_another_packages_tests_releases_nothing(self):
        """release-please reads a renamed file at its new path only, so this releases nothing.

        `b` excludes its tests and `a` is never attributed the old location, so a releasing
        title here would promise a changelog entry no package receives.
        """
        assert check.assess(
            "feat: move module",
            ["packages/b/tests/test_mod.py"],
            {},
            [("packages/a/src/mod.py", "packages/b/tests/test_mod.py")],
        )

    def test_readme_like_tool_name_is_not_documentation(self):
        assert not check.is_documentation_path("scripts/README-generator.sh")

    def test_fix_on_root_metadata_has_no_shipped_behavior(self):
        assert check.assess("fix: update dependency", ["pyproject.toml"], {})

    @pytest.mark.parametrize(
        "title",
        ["fix(core): correct the API", "feat(core): add the API", "feat(core)!: replace the API"],
    )
    def test_valid_scoped_and_breaking_prefixes_are_classified(self, title: str):
        assert check.title_type(title) in {"fix", "feat"}

    def test_breaking_behavior_title_is_classified_as_behavior(self):
        assert check.title_type("feat(core)!: change the API") == "feat"

    def test_renamed_python_file_uses_the_original_path_for_comparison(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        old_path = "scripts/old_name.py"
        new_path = tmp_path / "new_name.py"
        source = "def run() -> int:\n    return 1\n"
        new_path.write_text(source, encoding="utf-8")
        calls: list[tuple[str, ...]] = []

        def fake_git(*args: str) -> str:
            calls.append(args)
            if args[:3] == ("diff", "--find-renames", "--name-status"):
                return f"R100\t{old_path}\t{new_path}"
            assert args == ("show", f"base:{old_path}")
            return source

        monkeypatch.setattr(check, "_git", fake_git)
        result = check._changed_python("base")
        assert result[str(new_path)] == (source, source)
        assert check.assess(
            "chore: rename the module",
            ["packages/example/src/new_name.py"],
            result,
            [("packages/example/src/old_name.py", "packages/example/src/new_name.py")],
        )

    def test_python_rename_to_non_python_is_executable(self):
        assert check.assess(
            "docs: reorganize files",
            ["packages/example/src/example.txt"],
            {},
            [("packages/example/src/example.py", "packages/example/src/example.txt")],
        )

    def test_python_addition_with_empty_content_is_executable(self):
        assert check.assess(
            "chore: add package marker",
            ["packages/a/src/a.py"],
            {"packages/a/src/a.py": (None, "")},
        )

    def test_non_python_rename_to_python_does_not_parse_the_old_file(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        source = "def run() -> int:\n    return 1\n"

        def fake_git(*args: str) -> str:
            if args[:3] == ("diff", "--find-renames", "--name-status"):
                return "R100\tREADME.md\tscripts/example.py"
            raise AssertionError(args)

        monkeypatch.setattr(check, "_git", fake_git)
        result = check._changed_python("base")
        assert result["scripts/example.py"] == (None, None)
        assert check.assess(
            "docs: add module",
            ["packages/example/src/example.py"],
            {"packages/example/src/example.py": (None, source)},
            [("packages/example/README.md", "packages/example/src/example.py")],
        )


class TestTestsDoNotMakeAPackageTouched:
    """A package changed only in its tests is not releasing, so it owes no executable change.

    This is the gate's half of #629; `exclude-paths` in release-please-config.json is the
    other. They have to agree: the gate exists so a title and the changelog it becomes cannot
    disagree, and a rule applied on one side only puts them back in conflict.
    """

    _CORE = "packages/maf-sandbox/src/maf_sandbox/_protocol.py"
    _DEPENDENT_TEST = "packages/maf-sandbox-codeact/tests/test_codeact_workload.py"

    def test_a_core_feat_may_repair_a_dependents_suite(self):
        before, after = "def f() -> int:\n    return 1\n", "def f() -> int:\n    return 2\n"
        assert (
            check.assess(
                "feat(sandbox)!: a mandatory protocol member",
                [self._CORE, self._DEPENDENT_TEST],
                {self._CORE: (before, after), self._DEPENDENT_TEST: (before, after)},
            )
            == []
        )

    def test_a_behavior_title_over_tests_alone_still_fails(self):
        """The rule narrows what counts as touched; it does not stop asking for behavior."""
        before, after = "def f() -> int:\n    return 1\n", "def f() -> int:\n    return 2\n"
        problems = check.assess(
            "feat: a thing", [self._DEPENDENT_TEST], {self._DEPENDENT_TEST: (before, after)}
        )
        assert problems and "no executable change" in problems[0]

    def test_a_test_title_over_tests_alone_is_fine(self):
        before, after = "def f() -> int:\n    return 1\n", "def f() -> int:\n    return 2\n"
        assert (
            check.assess(
                "test(codeact): read the kind's own records",
                [self._DEPENDENT_TEST],
                {self._DEPENDENT_TEST: (before, after)},
            )
            == []
        )

    def test_a_tests_directory_inside_src_still_ships(self):
        """Only `packages/<name>/tests/` is excused, which is what the config excludes.

        A `tests` directory nested under `src/` is shipped code, and release-please attributes
        it, so excusing it here would let a `feat` past a package the changelog still names.
        """
        before, after = "def f() -> int:\n    return 1\n", "def f() -> int:\n    return 2\n"
        nested = "packages/maf-sandbox-codeact/src/maf_sandbox_codeact/tests/helper.py"
        assert check.assess(
            "feat: a thing",
            [self._CORE, nested],
            {self._CORE: (before, after), nested: (before, after)},
        )

    def test_a_dependents_source_still_makes_it_touched(self):
        """Only `tests/` is excused. A `feat` naming two packages still owes both."""
        before, after = "def f() -> int:\n    return 1\n", "def f() -> int:\n    return 2\n"
        assert check.assess(
            "feat: two packages",
            [self._CORE, "packages/maf-sandbox-codeact/README.md"],
            {self._CORE: (before, after)},
            [(self._CORE,), ("packages/maf-sandbox-codeact/README.md",)],
        )


class TestTheGeneratedReleasePullRequest:
    """Only a Release PR release-please opened is exempt, and it takes all four facts.

    Its diff is a version bump — `pyproject.toml`, `uv.lock`, the manifest — which this check
    reads as executable because it cannot read TOML, under a `chore(main): release …` title
    RELEASING.md forbids editing. Each fact rules out a different way of claiming to be one.
    """

    _RELEASE_PATHS = [
        ".release-please-manifest.json",
        "packages/maf-sandbox-bicep/CHANGELOG.md",
        "packages/maf-sandbox-bicep/pyproject.toml",
        "uv.lock",
    ]
    #: The same four files as `git diff --name-status` reports them.
    _STATUS = "\n".join(f"M\t{path}" for path in _RELEASE_PATHS)
    _REPO = "sokolaidev/maf-extensions"
    _BRANCH = "release-please--branches--main--components--maf-sandbox"
    _TITLE = "chore(main): release maf-sandbox 0.20.0"

    def _genuine(self, **overrides: str) -> bool:
        facts = {
            "head_ref": self._BRANCH,
            "head_repo": self._REPO,
            "base_repo": self._REPO,
            "author": "github-actions[bot]",
        } | overrides
        return check.is_generated_release(**facts)

    def test_the_diff_alone_is_still_refused(self):
        """The exemption is identity, never the shape of the diff.

        Pinned first because it is what makes the rest of this class mean anything: a person
        writing that title over that diff is exactly what the check exists to refuse.
        """
        assert check.assess("chore(main): release maf-sandbox-bicep 0.9.1", self._RELEASE_PATHS, {})

    def test_a_release_pull_request_is_exempt(self, monkeypatch):
        """Driven through `main`, so the guard is pinned wired rather than merely present."""
        monkeypatch.setattr(check, "_git", lambda *_args: self._STATUS)
        assert self._genuine()
        assert (
            check.main(
                [
                    "check",
                    "BASE",
                    self._TITLE,
                    "--head-ref",
                    self._BRANCH,
                    "--head-repo",
                    self._REPO,
                    "--base-repo",
                    self._REPO,
                    "--author",
                    "github-actions[bot]",
                ]
            )
            == 0
        )

    def test_a_fork_cannot_claim_it_by_naming_its_branch(self):
        """A branch name is chosen by whoever pushes it, and a fork may push any name.

        Nothing about that choice appears in the diff, so a reviewer reading the change sees
        no reason it went unchecked. The repository is the fact a fork cannot forge.
        """
        assert not self._genuine(head_repo="attacker/maf-extensions")

    def test_a_collaborator_cannot_claim_it_by_naming_a_branch_in_this_repository(self):
        """Same repository, same branch shape, a person's login — still checked."""
        assert not self._genuine(author="antsok")

    def test_the_range_pull_request_is_not_exempt(self):
        """`chore/maf-sandbox-range-…` moves dependency bounds, which is a behavior change.

        RELEASING.md requires it be titled `fix:` — "the type is load-bearing", because a
        ceiling widened under `chore:` releases nothing and the release sequence stalls with
        the publication window still open. The same bot opens it, so the branch prefix is the
        only fact separating the two and it has to keep doing that work.
        """
        bounds = ["packages/maf-sandbox-bicep/pyproject.toml"]
        assert not self._genuine(head_ref="chore/maf-sandbox-range-0.20.0")
        assert check.assess("chore: widen the ceilings", bounds, {})
        assert check.assess("fix: widen the ceilings", bounds, {}) == []

    @pytest.mark.parametrize(
        "head_ref", ["", "feat/something", "main", "my-release-please--branch"]
    )
    def test_an_ordinary_branch_is_not_exempt(self, head_ref: str):
        assert not self._genuine(head_ref=head_ref)

    @pytest.mark.parametrize(
        "head_ref",
        ["release-please--manual", "release-please--anything", "release-please--branches"],
    )
    def test_a_near_miss_in_the_namespace_is_not_exempt(self, head_ref: str):
        """The prefix is the whole namespace release-please generates, not a lead-in to it.

        `release-please--anything` is not a name it produces, so treating one as generated
        exempts a pull request nobody generated — and the bot identity does not catch it,
        because any workflow in this repository can open a branch under that author.
        """
        assert not self._genuine(head_ref=head_ref)

    @pytest.mark.parametrize("missing", ["head_ref", "head_repo", "base_repo", "author"])
    def test_every_fact_is_required(self, missing: str):
        """Any one of them empty fails closed, which is what makes the default safe."""
        assert not self._genuine(**{missing: ""})

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["check", "--help"], 0),
            (["check", "--not-a-flag"], 2),
            (["check"], 2),
            (["check", "BASE"], 2),
        ],
    )
    def test_the_exit_code_argparse_chose_is_the_one_returned(
        self, argv: list[str], expected: int, capsys
    ):
        """`--help` succeeds and a bad argument fails, which are not the same outcome.

        Flattening both to 2 answers "how do I call this" with a failure, and hides a genuine
        usage error behind the same number a successful help prints.
        """
        assert check.main(argv) == expected
        capsys.readouterr()

    def test_an_absent_fact_checks_rather_than_skips(self, monkeypatch):
        """A caller that stops passing them gets the check back, not a free pass.

        The same diff and title as the exempt case above, so the only difference is what was
        passed — which is what makes the pair mean the exemption rather than the fixture.
        """
        monkeypatch.setattr(check, "_git", lambda *_args: self._STATUS)
        assert check.main(["check", "BASE", self._TITLE]) == 1
        assert check.main(["check", "BASE", self._TITLE, "--head-ref", self._BRANCH]) == 1


class TestBothReadsStartAtTheMergeBase:
    """A caller's base is whatever the pull request opened against, and it does not move.

    The path list reaches the branch point through the three dots. The `git show <rev>:<path>`
    snapshot names a revision and cannot, so it has to be handed the merge base itself —
    otherwise a file the base branch changed since is read at the base branch's version, and
    the AST comparison reports as this pull request's a change nobody here made.
    """

    @staticmethod
    def _record(calls: list[tuple[str, ...]]):
        def fake_git(*args: str) -> str:
            calls.append(args)
            if args[0] == "merge-base":
                return "BRANCHPOINT"
            if args[0] == "diff":
                return "M\tpackages/maf-sandbox/src/maf_sandbox/mod.py"
            if args[0] == "show":
                return "x = 1\n"
            raise AssertionError(f"unexpected git call: {args}")

        return fake_git

    def test_the_merge_base_is_resolved_from_the_base_the_caller_passed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(check, "_git", self._record(calls))
        monkeypatch.chdir(tmp_path)
        check.main(["check", "STALEBASE", "docs: a note"])
        assert ("merge-base", "STALEBASE", "HEAD") in calls

    @pytest.mark.parametrize("read", ["diff", "show"])
    def test_neither_read_is_handed_the_callers_base(
        self, read: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(check, "_git", self._record(calls))
        monkeypatch.chdir(tmp_path)
        check.main(["check", "STALEBASE", "docs: a note"])
        made = [call for call in calls if call[0] == read]
        assert made, f"no `git {read}` was made at all"
        assert not [call for call in made if any("STALEBASE" in arg for arg in call)]
        assert [call for call in made if any("BRANCHPOINT" in arg for arg in call)]

    def test_a_base_with_no_merge_base_is_used_as_it_stands(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """A shallow clone has none to find, and refusing to check at all would be worse."""
        calls: list[tuple[str, ...]] = []
        recorded = self._record(calls)

        def fake_git(*args: str) -> str:
            if args[0] == "merge-base":
                raise subprocess.CalledProcessError(128, args)
            return recorded(*args)

        monkeypatch.setattr(check, "_git", fake_git)
        monkeypatch.chdir(tmp_path)
        check.main(["check", "STALEBASE", "docs: a note"])
        assert [call for call in calls if any("STALEBASE" in arg for arg in call)]
        assert "no merge base" in capsys.readouterr().err
