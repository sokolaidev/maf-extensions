"""Regression tests for the PR title versus diff semantic guard."""

from __future__ import annotations

import importlib.util
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

    def test_cross_package_python_rename_counts_only_non_test_endpoints(self):
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


class TestTheGeneratedReleasePullRequest:
    """release-please's own Release PRs are exempt, and nothing adjacent to them is.

    Their diff is a version bump — `pyproject.toml`, `uv.lock`, the manifest — which this
    check reads as executable because it cannot read TOML, against a `chore(main): release …`
    title release-please writes and RELEASING.md forbids editing. Every one of them failed.
    """

    _RELEASE_PATHS = [
        ".release-please-manifest.json",
        "packages/maf-sandbox-bicep/CHANGELOG.md",
        "packages/maf-sandbox-bicep/pyproject.toml",
        "uv.lock",
    ]
    #: The same four files as `git diff --name-status` reports them.
    _STATUS = "\n".join(f"M\t{path}" for path in _RELEASE_PATHS)

    def test_the_diff_alone_is_still_refused(self):
        """The exemption is the branch, not the shape of the diff — so this still fails.

        Pinned first because it is what makes the rest of this class mean anything: a person
        writing that title on that diff by hand is exactly what the check is for.
        """
        assert check.assess("chore(main): release maf-sandbox-bicep 0.9.1", self._RELEASE_PATHS, {})

    @pytest.mark.parametrize(
        "head_ref",
        [
            "release-please--branches--main--components--maf-sandbox",
            "release-please--branches--main--components--maf-sandbox-bicep",
        ],
    )
    def test_a_release_branch_is_exempt(self, head_ref: str, monkeypatch):
        """Driven through `main` over the diff a Release PR actually carries.

        Asserting only on `is_generated_release` would pass with the guard unwired, which is
        the shape of this defect: the check itself was right and nothing called it.
        """
        monkeypatch.setattr(check, "_git", lambda *_args: self._STATUS)
        title = "chore(main): release maf-sandbox 0.20.0"
        assert check.is_generated_release(head_ref)
        assert check.main(["check", "BASE", title, head_ref]) == 0

    def test_the_range_pull_request_is_not_exempt(self):
        """`chore/maf-sandbox-range-…` moves dependency bounds, which is a behavior change.

        RELEASING.md requires it be titled `fix:` — "the type is load-bearing", because a
        ceiling widened under `chore:` releases nothing and the release sequence stalls with
        the publication window still open. This check is what holds it there, so the exemption
        must not reach it.
        """
        bounds = ["packages/maf-sandbox-bicep/pyproject.toml"]
        assert not check.is_generated_release("chore/maf-sandbox-range-0.20.0")
        assert check.assess("chore: widen the ceilings", bounds, {})
        assert check.assess("fix: widen the ceilings", bounds, {}) == []

    @pytest.mark.parametrize(
        "head_ref", ["", "feat/something", "main", "my-release-please--branch"]
    )
    def test_an_ordinary_branch_is_not_exempt(self, head_ref: str):
        assert not check.is_generated_release(head_ref)

    def test_a_missing_head_ref_checks_rather_than_skips(self, monkeypatch):
        """A workflow that stops passing the branch gets the check back, not a free pass.

        The same diff and the same title as the exempt case above, so the only difference is
        the argument — which is what makes the pair mean the exemption rather than the fixture.
        """
        monkeypatch.setattr(check, "_git", lambda *_args: self._STATUS)
        assert check.main(["check", "BASE", "chore(main): release maf-sandbox 0.20.0"]) == 1
