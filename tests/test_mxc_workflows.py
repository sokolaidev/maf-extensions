"""MXC qualification follows changes to every shared experiment module."""

from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.workflow


@pytest.mark.parametrize("name", ["remote", "accounting", "physical", "volume"])
def test_qualification_covers_shared_runtime_modules(name: str):
    workflow = ROOT / ".github" / "workflows" / f"mxc-{name}.yml"
    triggers = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]
    patterns = triggers["pull_request"]["paths"]
    modules = (ROOT / "scripts" / "experiments" / "mxc_session_patch").rglob("*.py")
    missing = [
        module.relative_to(ROOT).as_posix()
        for module in modules
        if not any(
            fnmatchcase(module.relative_to(ROOT).as_posix(), pattern) for pattern in patterns
        )
    ]
    assert not missing, f"{workflow.name} skips runtime changes: {missing}"
