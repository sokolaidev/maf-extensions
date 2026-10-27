"""Exercise native capacity qualification with real supervisors and a simulated guest."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    probe = importlib.import_module("scripts.experiments.mxc_files_patch.physical_probe")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

FAKE_NATIVE = r"""
import base64, contextlib, io, json, subprocess, sys
from pathlib import Path
from scripts.experiments.mxc_files_patch import shared_call
from scripts.experiments.mxc_files_patch.physical_probe import candidate_files
from scripts.experiments.mxc_files_patch import physical_probe as probe

def execute(helper, startup, work, request, before_start, check_active, **kwargs):
    assert helper.is_file()
    namespace = json.loads((startup / 'index.json').read_bytes())
    files = candidate_files(startup) if (startup / 'workspace.json').exists() else {}
    files.update({'session/' + item.name: item.data for item in request.inputs})
    def guest_open(name, mode):
        key = name.removeprefix('/workspace/') if name.startswith('/') else 'calls/fixture/' + name
        if mode == 'rb':
            if key not in files: raise FileNotFoundError(key)
            return io.BytesIO(files[key])
        class Output(io.BytesIO):
            def close(self):
                files[key] = self.getvalue()
                super().close()
        return Output()
    namespace['open'] = guest_open
    output = io.StringIO()
    kwargs.get('before_spawn', lambda: None)()
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], stdin=subprocess.PIPE) as child:
        try:
            before_start(child)
            check_active()
            with contextlib.redirect_stdout(output):
                exec(request.code, namespace)
            candidate = work / 'candidate'
            candidate.mkdir()
            (candidate / 'index.json').write_text(json.dumps({'physical_value': namespace['physical_value']}))
            entries, data = [], b''
            for name, value in sorted(files.items()):
                if name.startswith('session/'):
                    entries.append({'name': name, 'offset':len(data), 'bytes':len(value)})
                    data += value
            (candidate / 'workspace.json').write_text(json.dumps({'files':entries}))
            (candidate / 'workspace.bin').write_bytes(data)
            (work / 'native.json').write_text('{}')
        finally:
            child.kill()
            child.wait(timeout=10)
    encoded = lambda data: {'base64': base64.b64encode(data).decode()}
    return json.dumps({'streams':{'stdout':encoded(output.getvalue().encode()),'stderr':encoded(b'')},'artifacts':[{'name':'result.bin',**encoded(files['calls/fixture/result.bin'])}]}).encode()
"""


@pytest.mark.parametrize("filesystem", [False, True])
def test_capacity_failure_and_recovery_across_real_supervisors(tmp_path, monkeypatch, filesystem):
    helper = tmp_path / "helper"
    helper.write_bytes(b"offline fixture")
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "index.json").write_text("{}")
    worker = tmp_path / "worker.py"
    volume_fixture = (
        r"""
import errno, sqlite3
from scripts.experiments.mxc_files_patch import volume_probe as probe
from scripts.experiments.mxc_session_patch.shared_store import SharedStore
probe.volume_control.validate_volume = lambda *a, **kw: None
def fill(volume, **kwargs):
    (volume / 'filler.bin').write_bytes(b'simulated exhaustion')
    return {'full_free_bytes': 0, 'errno': errno.ENOSPC}
probe.volume_control.fill = fill
capture = SharedStore._capture
def fail_capture(self, call_id, root):
    if call_id == 'overflow':
        assert (self.root.parent / 'filler.bin').exists()
        error = sqlite3.OperationalError('simulated SQLITE_FULL')
        error.sqlite_errorcode = sqlite3.SQLITE_FULL
        raise error
    return capture(self, call_id, root)
SharedStore._capture = fail_capture
"""
        if filesystem
        else ""
    )
    selected = (
        importlib.import_module("scripts.experiments.mxc_files_patch.volume_probe")
        if filesystem
        else probe
    )
    worker.write_text(
        FAKE_NATIVE
        + volume_fixture
        + "\nshared_call.execute = execute\nraise SystemExit(probe.main())\n",
        encoding="utf-8",
    )
    namespace = {}
    exec(FAKE_NATIVE, namespace)
    monkeypatch.setattr(namespace["shared_call"], "execute", namespace["execute"])
    real_run = subprocess.run

    def launch(command, **kwargs):
        assert command[1:3] == ["-m", selected.__name__]
        return real_run(
            [sys.executable, str(worker), *command[3:]],
            env={**os.environ, "PYTHONPATH": str(Path.cwd())},
            **kwargs,
        )

    monkeypatch.setattr(selected.subprocess, "run", launch)
    if filesystem:
        volume = tmp_path / "volume"
        volume.mkdir()
        monkeypatch.setattr(selected.volume_control, "validate_volume", lambda *a, **kw: None)
        report = selected.qualify(helper, startup, tmp_path / "qualification", volume, "owner")
        assert not (volume / "filler.bin").exists()
        assert report["supervisors"]["overflow"]["filesystem"]["full_free_bytes"] == 0
    else:
        report = selected.qualify(helper, startup, tmp_path / "qualification")
        assert report["format"] == 6
        assert report["calibration_database_bytes"] >= report["calibration_seed_bytes"]
        assert (
            report["policy"]["database_bytes"]
            == report["calibration_database_bytes"] + probe.MARGIN
        )
        assert report["payload_bytes"] == 64 * 1024**2
    assert report["qualified"]
    records = report["supervisors"]
    assert len({record["pid"] for record in records.values()}) == 3
    assert records["overflow"]["sqlite_errorcode"] == sqlite3.SQLITE_FULL
    assert records["recover"]["python_and_session_files_recovered"]
    assert records["recover"]["scratch_reclaimed"]
    assert records["seed"]["seed_sha256"] == records["recover"]["replay_after_collection_sha256"]
    assert all(
        record["after"]["database_bytes"] <= report["policy"]["database_bytes"]
        for record in records.values()
    )


def test_launch_requires_worker_success_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(AssertionError):
        probe.launch(tmp_path, tmp_path, tmp_path, "overflow")
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(FileNotFoundError):
        probe.launch(tmp_path, tmp_path, tmp_path, "overflow")


@pytest.mark.workflow
def test_native_capacity_reports_retained_without_state_payloads():
    root = Path(__file__).parents[1]
    job = yaml.safe_load((root / ".github/workflows/mxc-native.yml").read_text(encoding="utf-8"))[
        "jobs"
    ]["mxc-files"]
    upload = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    paths = upload["with"]["path"].splitlines()
    assert "${{ runner.temp }}/mxc-files/physical/*.json" in paths
    assert "${{ runner.temp }}/mxc-files/physical/*.log" in paths
    assert not any(
        "physical/" in path and ("**" in path or ".bin" in path or "store" in path)
        for path in paths
    )


def test_native_volume_failure_frees_filler_without_claiming_qualification(tmp_path, monkeypatch):
    selected = importlib.import_module("scripts.experiments.mxc_files_patch.volume_probe")
    volume = tmp_path / "volume"
    volume.mkdir()
    helper = tmp_path / "helper"
    helper.write_bytes(b"fixture")
    state = tmp_path / "state"
    validations = []
    monkeypatch.setattr(
        selected.volume_control, "validate_volume", lambda *a, **kw: validations.append(a)
    )

    def launch(helper, startup, state, volume, token, stage):
        if stage == "seed":
            return {}
        (volume / "filler.bin").write_bytes(b"fixture")
        raise RuntimeError("worker failed")

    monkeypatch.setattr(selected, "launch", launch)
    with pytest.raises(RuntimeError, match="worker failed"):
        selected.qualify(helper, tmp_path, state, volume, "owner")
    assert len(validations) == 2
    assert not (volume / "filler.bin").exists()
    assert not json.loads((state / "result.json").read_bytes())["qualified"]


@pytest.mark.workflow
def test_native_volume_setup_cleanup_and_evidence():
    root = Path(__file__).parents[1]
    job = yaml.safe_load((root / ".github/workflows/mxc-native.yml").read_text(encoding="utf-8"))[
        "jobs"
    ]["mxc-files"]
    steps = job["steps"]
    create = next(s for s in steps if s.get("with", {}).get("phase") == "create")
    cleanup = next(s for s in steps if s.get("with", {}).get("phase") == "cleanup")
    qualify = next(s for s in steps if s.get("name") == "Qualify files, quota and durable replay")
    assert steps.index(create) < steps.index(qualify) < steps.index(cleanup)
    assert create["with"]["volume-mib"] == "8192"
    assert "always()" in cleanup["if"]
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    paths = upload["with"]["path"].splitlines()
    assert "${{ runner.temp }}/mxc-files/volume/*.json" in paths
    assert "${{ runner.temp }}/mxc-files/volume/*.log" in paths
    assert "${{ runner.temp }}/mxc-volume-results/cleanup.json" in paths
    assert not any("volume/" in p and ("**" in p or ".bin" in p or "store" in p) for p in paths)
