"""Reject source and dependency drift before producing a Terraform engine."""

import json
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "images/terraform-sandbox"
sys.path.insert(0, str(SOURCE))
import build_terraform  # noqa: E402


@pytest.mark.parametrize("mismatch", ["toolchain", "revision", "module", "replacement"])
def test_build_refuses_drift_before_compilation(tmp_path, monkeypatch, mismatch):
    plan = json.loads((SOURCE / "image.json").read_text(encoding="utf-8"))["engines"]["terraform"]
    source = plan["source_build"]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[:2] not in (["go", "test"], ["go", "build"])

    def output(command, **kwargs):
        if command == ["go", "env", "GOVERSION"]:
            return "go1.26.8" if mismatch == "toolchain" else source["go_version"]
        if command == ["git", "rev-parse", "HEAD"]:
            return "0" * 40 if mismatch == "revision" else source["revision"]
        assert command[:4] == ["go", "list", "-m", "-json"]
        selected = {"Version": source["modules"][command[4]]}
        if mismatch == "module":
            selected["Version"] = "v0.58.0"
        elif mismatch == "replacement":
            selected["Replace"] = {"Dir": "/unreviewed"}
        return json.dumps(selected)

    monkeypatch.setattr(build_terraform.subprocess, "run", run)
    monkeypatch.setattr(build_terraform.subprocess, "check_output", output)
    with pytest.raises(ValueError, match="mismatch"):
        build_terraform.build(plan, tmp_path / "checkout", tmp_path / "output")
    assert not (tmp_path / "output").exists()
    if mismatch == "toolchain":
        assert calls == []
    elif mismatch == "revision":
        assert len(calls) == 1 and calls[0][:2] == ["git", "clone"]
    else:
        assert ["go", "mod", "verify"] in calls
