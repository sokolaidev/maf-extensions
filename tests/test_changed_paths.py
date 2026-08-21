"""What a docs-only pull request is allowed to skip.

`scripts/changed_paths.py` decides whether the offline suite, both pyright passes, the wheel
builds and the Docker job run for a change. Getting it wrong in one direction wastes three
minutes; getting it wrong in the other ships a defect, so every case here is written from the
question "what would this let through".

The one that is not obvious: `packages/**` is code whatever its extension. A README under a
package is packaged and rendered on PyPI, so a change to it is a change to a published
artefact.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "changed_paths", _ROOT / "scripts" / "changed_paths.py"
)
assert _spec and _spec.loader
changed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changed)


class TestWhatCountsAsDocumentation:
    @pytest.mark.parametrize(
        "path",
        [
            "docs/sandbox/architecture.md",
            "docs/sandbox/assets/isolation-floor.svg",
            "README.md",
            "RELEASING.md",
            "samples/05_docker_bicep/README.md",
        ],
    )
    def test_a_file_no_wheel_carries_is_documentation(self, path: str):
        assert changed.is_documentation(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            # Packaged and rendered on PyPI: a change to it changes a published artefact.
            "packages/maf-sandbox/README.md",
            "packages/maf-sandbox/src/maf_sandbox/_router.py",
            "scripts/check_doc_paths.py",
            "tests/test_poe_gate.py",
            "pyproject.toml",
            "uv.lock",
            ".github/workflows/tests.yml",
            "samples/05_docker_bicep/agent.py",
        ],
    )
    def test_everything_else_is_code(self, path: str):
        assert changed.is_documentation(path) is False

    def test_a_windows_separator_is_read_the_same_way(self):
        """`git diff --name-only` gives forward slashes, but nothing downstream should depend
        on the caller having been git."""
        assert changed.is_documentation("docs\\sandbox\\architecture.md") is True


class TestTheVerdict:
    def test_a_documentation_only_change_skips_the_code_checks(self):
        assert changed.runs_code_checks(["docs/sandbox/architecture.md", "README.md"]) is False

    def test_one_code_file_among_many_documents_runs_everything(self):
        """The expensive direction is the safe one, so a single source file is decisive."""
        paths = ["docs/a.md", "README.md", "packages/maf-sandbox/src/maf_sandbox/_router.py"]
        assert changed.runs_code_checks(paths) is True

    def test_a_packaged_readme_runs_everything(self):
        assert changed.runs_code_checks(["packages/maf-sandbox/README.md"]) is True

    def test_an_empty_diff_runs_everything(self):
        """A diff that came back empty is more likely a base that could not be resolved than a
        pull request that changed nothing, and the two are indistinguishable from here."""
        assert changed.runs_code_checks([]) is True

    def test_blank_lines_are_not_paths(self):
        assert changed.runs_code_checks(["", "  ", "docs/a.md"]) is False


class TestTheOutputLine:
    """The workflow reads this straight into `$GITHUB_OUTPUT`, so its shape is the contract."""

    def test_documentation_only_prints_false(self, capsys, monkeypatch):
        monkeypatch.setattr(changed.sys, "stdin", io.StringIO("docs/a.md\nREADME.md\n"))
        assert changed.main(["changed_paths.py"]) == 0
        assert capsys.readouterr().out == "code=false\n"

    def test_a_code_change_prints_true(self, capsys, monkeypatch):
        monkeypatch.setattr(changed.sys, "stdin", io.StringIO("packages/p/src/m/_x.py\n"))
        assert changed.main(["changed_paths.py"]) == 0
        assert capsys.readouterr().out == "code=true\n"

    def test_an_argument_is_refused(self, capsys):
        assert changed.main(["changed_paths.py", "extra"]) == 2
