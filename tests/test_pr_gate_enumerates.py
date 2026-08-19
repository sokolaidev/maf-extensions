"""The PR gate enumerates packages; a list is how the last one went unchecked (#450).

`tests.yml`'s type-check, build and smoke steps each named six packages one line each, and a
seventh would have been invisible until someone remembered to add it — which is exactly how
`maf-sandbox-wslc` shipped without a conformance check of any kind. These steps now loop over
`packages/*/`; this file pins that they keep looping, because a rule that matches nothing
passes every time.

**Scoped to `tests.yml`.** `publish-packages.yml` names packages in its tag patterns and its
dispatch options, and neither can be enumerated: a tag pattern is a filter GitHub matches
against, not a list this repository controls the expansion of. Those are declared out of scope
here rather than silently unmatched.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_YML = REPO_ROOT / ".github" / "workflows" / "tests.yml"
_TEXT = TESTS_YML.read_text(encoding="utf-8")

#: A package name spelled out where a loop variable should be — `maf-sandbox` (the prefix every
#: package shares) with anything but `*` following, or a bare `packages/maf-sandbox-<name>`
#: path. The loop forms name no package: `packages/*/`, `$(dirname …)`, `$(basename …)`.
_HARDCODED_PACKAGE = re.compile(
    r"(?<![-\w*/])(?:packages/)?maf-sandbox-(?:acas|bicep|codeact|docker|wslc)(?![\w/-])"
)


class TestTheGateEnumerates:
    def test_the_workflow_still_has_its_three_steps(self):
        """Without this the tests below pass vacuously on a file that stopped gating anything."""
        for step in (
            "Types (per-package strict configs)",
            "Build + metadata check",
            "Install + use each wheel in a clean environment",
        ):
            assert f"name: {step}" in _TEXT, f"tests.yml lost its {step!r} step"

    def test_the_steps_loop_rather_than_list(self):
        for step in ("per-package strict configs", "Build + metadata check", "clean environment"):
            block = _step_block(step)
            assert "packages/*/" in block or "packages/*/pyproject.toml" in block, (
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
            # Walk forward to this step's `run: |` and collect its indented body.
            for cursor in range(index + 1, len(lines)):
                if lines[cursor].lstrip().startswith("run:"):
                    body: list[str] = []
                    for body_line in lines[cursor + 1 :]:
                        if body_line.strip() and not body_line.startswith(" " * 10):
                            break
                        body.append(body_line)
                    return "\n".join(body)
    raise AssertionError(f"tests.yml has no step naming {fragment!r}")
