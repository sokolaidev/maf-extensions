"""Pin the poe gate's composition: the tasks exist, and `gate` runs every one of them.

No count is written down. `_GATE_MEMBERS` is the list, and a number repeated in prose here
would be a second copy that no assertion checks.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The gate's discovery helper sits in scripts/, on no package's path; extraPaths in the root
# pyproject resolves it for the checker and this line for pytest — the same two-step the
# sample-09 import in test_no_isolation_backend.py takes.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from gate_tasks import packages_with_pyright  # noqa: E402

_PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_TASKS = _PYPROJECT["tool"]["poe"]["tasks"]

_GATE_MEMBERS = ["test", "lint", "format", "types-packages", "types", "doc-paths"]


class TestTheGate:
    def test_the_gate_contains_every_check(self):
        assert _TASKS["gate"]["sequence"] == _GATE_MEMBERS, (
            f"poe gate runs {_TASKS['gate']['sequence']}; the pre-PR gate is {_GATE_MEMBERS}. "
            "A check dropped from the sequence is a gate that passes without it."
        )

    def test_the_simple_tasks_are_the_canonical_commands(self):
        assert _TASKS["test"] == "pytest -q"
        assert _TASKS["lint"] == "ruff check ."
        assert _TASKS["format"] == "ruff format --check ."
        assert _TASKS["types"] == "pyright"

    def test_the_documentation_check_runs_the_script_it_names(self):
        """Naming a member in the sequence does not make it exist.

        `doc-paths` is a table rather than a string, so its wiring is a second value nothing
        above reaches: delete the table or point `cmd` elsewhere and the sequence assertion
        stays green while `uv run poe gate` checks no documentation at all.
        """
        assert _TASKS["doc-paths"]["cmd"] == "python scripts/check_doc_paths.py", _TASKS[
            "doc-paths"
        ]

    def test_the_enumerated_pass_is_wired_to_the_helper_it_is_pinned_against(self):
        """The task below pins what the helper *finds*; nothing pinned that poe calls it.

        `types-packages` is the one gate member that runs a script rather than a command, so
        its wiring is two values a test can check and neither was checked: a `script` pointing
        elsewhere, or a lost `PYTHONPATH`, leaves `uv run poe gate` no longer checking any
        package while every test here stays green.
        """
        task = _TASKS["types-packages"]
        assert task["script"] == "gate_tasks:pyright_packages", task
        assert task["env"]["PYTHONPATH"] == "scripts", task

    def test_the_enumerated_pass_finds_every_package(self):
        """The same discovery the workflow's loop performs, so the two cannot drift apart.

        `tests/test_pr_gate_enumerates.py` pins that CI loops; this pins that the loop and the
        local task enumerate the same set — six packages today, and the seventh on the commit
        that adds it.
        """
        found = sorted(path.name for path in packages_with_pyright())
        assert found == [
            "maf-sandbox",
            "maf-sandbox-acas",
            "maf-sandbox-bicep",
            "maf-sandbox-codeact",
            "maf-sandbox-docker",
            "maf-sandbox-wslc",
        ], found


class TestTheMarkdownBlockLinter:
    """The poe task and the CI step must lint the same files.

    The glob list is written twice — once in `pyproject.toml`, once in `tests.yml` — and
    nothing but this test stops the two drifting. A contributor's green local run is only
    evidence about CI if both read the same set, and the failure is silent in the direction
    that matters: CI quietly covering *less* than the task a contributor ran.
    """

    @staticmethod
    def _globs(command: str) -> set[str]:
        """The arguments after the script name, with shell quoting and continuations removed."""
        _before, _, arguments = command.partition("check_md_code_blocks.py")
        cleaned = arguments.replace("\\", " ").replace("\n", " ")
        return {token.strip("'\"") for token in cleaned.split() if token.strip("'\"")}

    def test_the_task_and_the_workflow_lint_the_same_globs(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        step = workflow[workflow.index("check_md_code_blocks.py") :]
        # The invocation ends at the blank line before the next step.
        invocation = step[: step.index("\n\n")]
        assert self._globs(_TASKS["md-blocks"]["cmd"]) == self._globs(invocation), (
            "poe md-blocks and the tests.yml step lint different files; a local run then says "
            "nothing about what CI checked."
        )

    def test_the_decided_documentation_is_covered(self):
        """`docs/` was outside this linter until the restructure moved the documentation there.

        `docs/sandbox/research/` stays out on purpose: a proposal describes an API that does not
        exist yet, so linting its snippets against the installed packages would report a design
        document for being a design document.
        """
        globs = self._globs(_TASKS["md-blocks"]["cmd"])
        assert "docs/sandbox/*.md" in globs
        assert not any(glob.startswith("docs/sandbox/research") for glob in globs)
