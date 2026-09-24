"""Select CI image builds while dependents adopt a new core release line."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

from pypi_index import admits, version

ROOT = Path(__file__).resolve().parent.parent
DEPENDENTS = ("maf-sandbox-hyperlight", "maf-sandbox-codeact")
_RANGE = re.compile(r"maf-sandbox>=(\d+(?:\.\d+)*),<(\d+(?:\.\d+)*)")


def pending_adoptions(root: Path) -> list[str]:
    """Return dependents capped at the current core line; reject other incompatible metadata."""
    packages = root / "packages"
    core_text = tomllib.loads((packages / "maf-sandbox/pyproject.toml").read_text("utf-8"))[
        "project"
    ]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", core_text):
        raise ValueError(f"unsupported core release version: {core_text!r}")
    core = version(core_text)
    pending = []
    for name in DEPENDENTS:
        project = tomllib.loads((packages / name / "pyproject.toml").read_text("utf-8"))["project"]
        requirements = [
            dependency
            for dependency in project["dependencies"]
            if re.match(r"maf[-_]sandbox(?![-_\w])", dependency, re.IGNORECASE)
        ]
        if len(requirements) != 1 or not (match := _RANGE.fullmatch(requirements[0])):
            raise ValueError(f"{name}: expected one maf-sandbox>=X,<Y requirement")
        floor, ceiling = (version(bound) for bound in match.groups())
        requirement = requirements[0]
        if not admits(floor, ceiling) or admits(core, floor):
            raise ValueError(f"{name}: {requirement} cannot use core {core_text}")
        if admits(core, ceiling):
            continue
        if admits(ceiling, core[:2]) or admits(core[:2], ceiling):
            raise ValueError(f"{name}: {requirement} is behind core release line {core_text}")
        pending.append(f"{name} requires {requirement}; checkout core is {core_text}")
    return pending


def main() -> None:
    """Emit a workflow output and explain any deferred image in the job summary."""
    pending = pending_adoptions(ROOT)
    if pending:
        report = (
            "Hyperlight image build deferred until dependents adopt the current core line.\n\n"
            + "\n".join(f"- {reason}" for reason in pending)
            + "\n\nNo image was built or verified. Linux worker and KVM checks remain enabled.\n"
        )
    else:
        report = "Hyperlight workspace core ranges admit an image build.\n"
    print(report, file=sys.stderr)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(report)
    print(f"build={str(not pending).lower()}")


if __name__ == "__main__":
    main()
