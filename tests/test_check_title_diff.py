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
        assert "retitle as feat:, fix:, perf:, or revert:" in problems[1]
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
