"""Exercise native capacity qualification with real supervisors and a simulated guest."""

from __future__ import annotations

import importlib
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
from scripts.experiments.mxc_files_patch import physical_probe as probe, shared_call

def execute(helper, startup, work, request, before_start, check_active, **kwargs):
    assert helper.is_file()
    namespace = json.loads((startup / 'index.json').read_bytes())
    files = probe.candidate_files(startup) if (startup / 'workspace.json').exists() else {}
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


def test_capacity_failure_and_recovery_across_real_supervisors(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_bytes(b"offline fixture")
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "index.json").write_text("{}")
    worker = tmp_path / "worker.py"
    worker.write_text(
        FAKE_NATIVE + "\nshared_call.execute = execute\nraise SystemExit(probe.main())\n",
        encoding="utf-8",
    )
    namespace = {}
    exec(FAKE_NATIVE, namespace)
    monkeypatch.setattr(namespace["shared_call"], "execute", namespace["execute"])
    real_run = subprocess.run

    def launch(command, **kwargs):
        assert command[1:3] == ["-m", "scripts.experiments.mxc_files_patch.physical_probe"]
        return real_run(
            [sys.executable, str(worker), *command[3:]],
            env={**os.environ, "PYTHONPATH": str(Path.cwd())},
            **kwargs,
        )

    monkeypatch.setattr(probe.subprocess, "run", launch)
    report = probe.qualify(helper, startup, tmp_path / "qualification")
    assert report["qualified"] and report["format"] == 6
    records = report["supervisors"]
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
    job = yaml.safe_load((root / ".github/workflows/tests.yml").read_text(encoding="utf-8"))[
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
