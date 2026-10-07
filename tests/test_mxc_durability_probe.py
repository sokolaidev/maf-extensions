"""Exercise crash qualification without a native guest and reject false runner success."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    probe = importlib.import_module("scripts.experiments.mxc_session_patch.durability_probe")
    runner = importlib.import_module("scripts.experiments.mxc_session_patch.qualification_runner")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

FAKE_NATIVE = """
import contextlib, io, json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from scripts.experiments.mxc_session_patch import durability_probe as probe, shared_call
probe.LIMITS = probe.Limits(20*1024**2, 40*1024**2, checkpoint_bytes=1024, files=2)
def execute(helper, startup, work, code, limit, before_start, checkpoint_limits):
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], stdin=subprocess.PIPE) as child:
        try:
            before_start(child)
            namespace = json.loads((startup / 'index.json').read_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(code, namespace)
            candidate = work / 'candidate'
            candidate.mkdir()
            (candidate / 'index.json').write_text(json.dumps({'mxc_durable_value': namespace['mxc_durable_value']}))
        finally:
            child.kill()
            child.wait(timeout=10)
    import base64
    return json.dumps({'console_base64': base64.b64encode(output.getvalue().encode()).decode()}).encode()
shared_call.execute = execute
raise SystemExit(probe.main())
"""


def test_complete_crash_and_quota_matrix_with_real_supervisor_processes(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_bytes(b"offline fixture")
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "index.json").write_text("{}")
    worker = tmp_path / "worker.py"
    worker.write_text(FAKE_NATIVE, encoding="utf-8")
    monkeypatch.setattr(
        probe, "LIMITS", probe.Limits(20 * 1024**2, 40 * 1024**2, checkpoint_bytes=1024, files=2)
    )
    real_run = subprocess.run

    def launch(command, **kwargs):
        assert command[1:3] == ["-m", "scripts.experiments.mxc_session_patch.durability_probe"]
        return real_run([sys.executable, str(worker), *command[3:]], **kwargs)

    monkeypatch.setattr(probe.subprocess, "run", launch)
    result = probe.qualify(helper, startup, tmp_path / "qualification")
    assert result["qualified"] is True
    assert result["format"] == 4
    assert {item["boundary"] for item in result["boundaries"]} == set(probe.BOUNDARIES)
    assert set(result["quota_controls"]) == {"session", "store"}
    assert result["replay_after_collection"] is True


def test_launch_refuses_a_worker_that_did_not_reach_the_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        probe.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    with pytest.raises(AssertionError):
        probe._launch(tmp_path, tmp_path, tmp_path, "crash", 2, "before_commit")


@pytest.mark.parametrize("preflight", [False, True])
def test_runner_never_qualifies_missing_hypervisor(tmp_path, monkeypatch, preflight):
    root = tmp_path / "qualification"
    if not preflight:
        root.mkdir()

    def absent():
        raise RuntimeError("unavailable hypervisor")

    monkeypatch.setattr(runner, "hypervisor", absent)
    monkeypatch.setattr(
        sys, "argv", ["runner", "--root", str(root), *(["--preflight"] if preflight else [])]
    )
    assert runner.main() == 1
    report = json.loads((root / ("preflight.json" if preflight else "result.json")).read_text())
    assert report["status"] == "unqualified"
    assert report["reason"] == "unavailable hypervisor"


def test_runner_failed_build_preserves_failure_and_never_qualifies(tmp_path, monkeypatch):
    root = tmp_path / "qualification"
    root.mkdir()
    monkeypatch.setattr(runner, "hypervisor", lambda: "KVM")

    def failed(*args):
        raise RuntimeError("native probe failed")

    monkeypatch.setattr(runner, "_build_and_probe", failed)
    monkeypatch.setattr(sys, "argv", ["runner", "--root", str(root)])
    assert runner.main() == 1
    report = json.loads((root / "result.json").read_text())
    assert report["status"] == "unqualified"
    assert report["reason"] == "native probe failed"


@pytest.mark.workflow
def test_native_qualification_is_opt_in_and_keeps_reports_without_checkpoints():
    path = Path(__file__).parents[1] / ".github/workflows/tests.yml"
    job = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["mxc-durability"]
    assert "if" not in job
    assert all("inputs.mxc_durability" in step["if"] for step in job["steps"])
    assert job["strategy"]["fail-fast"] is False
    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-24.04", "windows-latest"}
    steps = {step.get("name"): step for step in job["steps"]}
    assert "--preflight" in steps["Require the native hypervisor"]["run"]
    assert "continue-on-error" not in steps["Require the native hypervisor"]
    upload = steps["Retain qualification reports and logs"]
    assert upload["if"].startswith("always() &&")
    assert "store" not in upload["with"]["path"]
    assert "**" not in upload["with"]["path"]
