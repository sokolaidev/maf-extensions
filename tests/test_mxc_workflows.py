"""MXC qualification follows changes to every shared experiment module."""

from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.workflow


@pytest.mark.parametrize("name", ["remote", "physical", "volume"])
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


def load_workflow(name: str):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def test_native_qualification_is_separate_from_the_required_package_gate():
    native = load_workflow("mxc-native.yml")
    assert set(native.get("on", native.get(True))) == {"workflow_dispatch"}
    expected = {
        "mxc-v1": "inputs.mxc_v1 != 'off'",
        "mxc-stream-kernel": "inputs.mxc_stream_kernel || inputs.mxc_streams",
        "mxc-streams": "inputs.mxc_streams",
        "mxc-file-kernel": "inputs.mxc_files",
        "mxc-files": "inputs.mxc_files",
    }
    assert set(native["jobs"]) == set(expected)
    for name, condition in expected.items():
        job = native["jobs"][name]
        assert job["if"] == condition
        assert not job.get("continue-on-error", False)
    assert native["jobs"]["mxc-streams"]["needs"] == ["mxc-stream-kernel"]
    assert native["jobs"]["mxc-files"]["needs"] == ["mxc-file-kernel"]
    for name in ("mxc-v1", "mxc-stream-kernel", "mxc-file-kernel"):
        assert not native["jobs"][name].get("needs")
    gate = load_workflow("tests.yml")
    inputs = gate.get("on", gate.get(True))["workflow_dispatch"]["inputs"]
    assert not any("mxc" in key for key in inputs)
    assert not any("mxc" in key for key in gate["jobs"])
    assert set(gate["jobs"]["tests"]["needs"]) == set(gate["jobs"]) - {"tests"}


def test_accounting_and_openclaw_require_manual_dispatch():
    for name in ("mxc-accounting.yml", "openclaw-mxc.yml"):
        workflow = load_workflow(name)
        assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    openclaw = load_workflow("openclaw-mxc.yml")
    assert openclaw["jobs"]["openclaw-mxc"]["if"] == "inputs.openclaw_mxc"
    assert not openclaw["jobs"]["openclaw-mxc"].get("needs")
    assert not (ROOT / ".github/workflows/unikraft-output.yml").exists()
