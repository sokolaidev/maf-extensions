"""Pin that `tests.yml`'s type-check, build and smoke steps enumerate packages rather than
name them, and that the type-check step runs the shared `gate_tasks` discovery.

Out of scope on purpose: `publish-packages.yml`'s tag patterns and dispatch options name
packages, and a tag pattern is a filter GitHub matches rather than a list this repository
expands.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The gate's discovery helper sits in scripts/, on no package's path; extraPaths in the root
# pyproject resolves it for the checker and this line for pytest — the same two-step the
# sample-09 import in test_no_isolation_backend.py takes.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from gate_tasks import packages_with_pyright  # noqa: E402

TESTS_YML = REPO_ROOT / ".github" / "workflows" / "tests.yml"
_TEXT = TESTS_YML.read_text(encoding="utf-8")

#: A package name spelled out where a loop variable should be. Generic rather than an
#: enumeration of today's five suffixes: it matches the bare `maf-sandbox` and any future
#: `maf-sandbox-<anything>`, because the regression this guards is *any* hardcoded name — the
#: next backend's name is exactly the one a five-name list would miss. The loop forms name no
#: package: `packages/*/`, `$(basename …)`.
_HARDCODED_PACKAGE = re.compile(r"(?<![-\w*/])(?:packages/)?maf-sandbox(?:-\w+)?(?![\w/-])")


class TestTheGateEnumerates:
    def test_the_workflow_still_has_its_three_steps(self):
        """Without this the tests below pass vacuously on a file that stopped gating anything."""
        for step in (
            "Types (per-package strict configs)",
            "Build + metadata check",
            "Install + use each wheel in a clean environment",
        ):
            assert f"name: {step}" in _TEXT, f"tests.yml lost its {step!r} step"

    def test_the_types_step_uses_the_shared_discovery(self):
        """One parser, not two: the step calls the module the local gate and this file use.

        A grep of the TOML is a second discovery implementation that drifts green on valid
        formatting it does not recognise — leading whitespace before `[tool.pyright]` — while
        `packages_with_pyright()` keeps working.
        """
        block = _step_block("per-package strict configs")
        assert "scripts/gate_tasks.py pyright-packages" in block, (
            "the per-package types step no longer runs scripts/gate_tasks.py — the shared "
            "tomllib discovery. A re-parse of its own (a grep, a fresh loop) is how CI and the "
            "local gate drift apart on what counts as a package (#450)."
        )

    def test_the_shared_discovery_answers_the_cli(self):
        """The entry point the workflow calls exists and enumerates every package."""
        import gate_tasks

        assert hasattr(gate_tasks, "main"), "gate_tasks.py lost its CLI entry point"
        found = sorted(path.name for path in packages_with_pyright())
        assert found == [
            "maf-sandbox",
            "maf-sandbox-acas",
            "maf-sandbox-bicep",
            "maf-sandbox-codeact",
            "maf-sandbox-docker",
            "maf-sandbox-wslc",
        ], found

    def test_the_steps_loop_rather_than_list(self):
        for step in ("Build + metadata check", "clean environment"):
            block = _step_block(step)
            assert "packages/*/" in block, (
                f"the {step!r} step no longer enumerates packages/*/ — it was a hardcoded list "
                "of six that let a seventh ship unchecked (#450), and the enumeration is what "
                "closes that"
            )

    def test_no_step_names_a_package_where_a_loop_belongs(self):
        """A package name inside the three enumerated steps is the regression this guards.

        The loop bodies may not name a package: `$(basename …)` and `$(dirname …)` are the
        only shapes that keep a new package covered on the commit that adds it.
        """
        for step in ("per-package strict configs", "Build + metadata check", "clean environment"):
            block = _step_block(step)
            named = sorted(set(_HARDCODED_PACKAGE.findall(block)))
            assert not named, (
                f"the {step!r} step names {named} explicitly. Enumerate packages/*/ instead: "
                "the list is how maf-sandbox-wslc went unchecked, and a new package would be "
                "too (#450)."
            )


def _step_block(fragment: str) -> str:
    """The `run:` body of the step whose name contains `fragment`."""
    lines = _TEXT.splitlines()
    for index, line in enumerate(lines):
        if f"name: {fragment}" in line or (
            fragment in line and line.lstrip().startswith("- name:")
        ):
            # Walk forward to this step's `run:` and collect its body: a single-line `run:`
            # command, or an indented block after `run: |`.
            for cursor in range(index + 1, len(lines)):
                if lines[cursor].lstrip().startswith("run:"):
                    body = [lines[cursor]]
                    for body_line in lines[cursor + 1 :]:
                        if body_line.strip() and not body_line.startswith(" " * 10):
                            break
                        body.append(body_line)
                    return "\n".join(body)
    raise AssertionError(f"tests.yml has no step naming {fragment!r}")
