"""Pin the poe gate: the tasks exist, and `gate` runs all five checks (#450).

A task renamed or dropped from `gate`'s sequence is the failure mode this catches — a
contributor running `uv run poe gate` before a PR would get a green gate that silently skipped
the format check or a pyright pass, which is the local mirror of the hardcoded-CI-list problem
`test_pr_gate_enumerates.py` guards on the workflow side.
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

_GATE_MEMBERS = ["test", "lint", "format", "types-packages", "types"]


class TestTheGate:
    def test_the_gate_contains_all_five_checks(self):
        assert _TASKS["gate"]["sequence"] == _GATE_MEMBERS, (
            f"poe gate runs {_TASKS['gate']['sequence']}; the pre-PR gate is {_GATE_MEMBERS}. "
            "A check dropped from the sequence is a gate that passes without it."
        )

    def test_the_simple_tasks_are_the_canonical_commands(self):
        assert _TASKS["test"] == "pytest -q"
        assert _TASKS["lint"] == "ruff check ."
        assert _TASKS["format"] == "ruff format --check ."
        assert _TASKS["types"] == "pyright"

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
