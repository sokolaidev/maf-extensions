"""Exercise checkpoint-copy failure orchestration without filling a local volume."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_mxc_physical_probe import FAKE_NATIVE

probe = importlib.import_module("scripts.experiments.mxc_files_patch.checkpoint_volume_probe")

FIXTURE = r"""
from scripts.experiments.mxc_files_patch import checkpoint_volume_probe as copied, transport
from scripts.experiments.mxc_session_patch.host_store import Refused
import errno
copied.volume_control.validate_volume = lambda *a, **kw: None

def pressure(volume, state, release=0):
    (volume / 'filler.bin').write_bytes(b'fixture')
    report = {'full_free_bytes':0,'errno':errno.ENOSPC}
    if release: report['released_free_bytes'] = release
    return report
copied.pressure = pressure
shared_call.execute = execute

def exporting(*args, **kwargs):
    kwargs.setdefault("check_active", lambda: None)
    result = execute(*args, **kwargs)
    work = args[2]
    if (work.parents[2] / 'filler.bin').exists():
        (work / 'candidate/index.json').unlink()
        (work / 'native.json').unlink()
        (work / 'native.stdout.bin').write_bytes(b'999')
        code = 112 if sys.platform == 'win32' else 28
        (work / 'native.stderr').write_text(f'os error {code}')
        raise Refused('native call failed: 1')
    return result
transport.execute = exporting
real_open = Path.open

def guarded_open(path, mode='r', *args, **kwargs):
    stream = real_open(path, mode, *args, **kwargs)
    if mode != 'xb' or 'restored' not in path.parts or 'scratch' not in path.parts:
        return stream
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): return stream.__exit__(*args)
        def write(self, data):
            volume = next(p.parent for p in path.parents if p.name == 'store')
            if (volume / 'filler.bin').exists(): raise OSError(errno.ENOSPC, 'fixture')
            return stream.write(data)
    return Stream()
Path.open = guarded_open
raise SystemExit(copied.main())
"""


def test_export_recovers_but_unidentified_restore_remains_charged(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_bytes(b"fixture")
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "index.json").write_text("{}")
    volume = tmp_path / "volume"
    volume.mkdir()
    script = tmp_path / "worker.py"
    script.write_text(FAKE_NATIVE + FIXTURE, encoding="utf-8")
    real_run = subprocess.run

    def launch(command, **kwargs):
        assert command[1:3] == ["-m", probe.__name__]
        return real_run(
            [sys.executable, str(script), *command[3:]],
            env={**os.environ, "PYTHONPATH": str(Path.cwd())},
            **kwargs,
        )

    monkeypatch.setattr(probe.subprocess, "run", launch)
    monkeypatch.setattr(probe.volume_control, "validate_volume", lambda *a, **kw: None)
    report = probe.qualify(helper, startup, tmp_path / "state", volume, "owner")
    assert report["qualified"]
    export = report["cases"]["export"]
    restore = report["cases"]["restore"]
    assert export["fail"]["incomplete_candidate"]
    assert export["recover"]["scratch_reclaimed"]
    assert export["recover"]["python_and_session_files_recovered"]
    assert restore["fail"]["partial_restore_bytes"] > 0
    assert restore["recover"]["unidentified_reclaim_refused"]
    assert restore["recover"]["reservation_retained"]
    assert restore["recover"]["new_call_refused"]
    assert not (volume / "filler.bin").exists()


@pytest.mark.parametrize("code", [0, 1])
def test_worker_must_succeed_and_write_report(tmp_path, monkeypatch, code):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=code))
    with pytest.raises(AssertionError if code else FileNotFoundError):
        probe.launch(tmp_path, tmp_path, tmp_path, tmp_path, "owner", "export", "fail")


def test_failed_worker_removes_filler_and_never_claims_qualification(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    volume.mkdir()
    helper = tmp_path / "helper"
    helper.write_bytes(b"fixture")
    monkeypatch.setattr(probe.volume_control, "validate_volume", lambda *a, **kw: None)

    def launch(helper, startup, state, volume, token, mode, stage):
        if stage == "seed":
            return {}
        (volume / "filler.bin").write_bytes(b"fixture")
        raise RuntimeError("fixture")

    monkeypatch.setattr(probe, "launch", launch)
    with pytest.raises(RuntimeError, match="fixture"):
        probe.qualify(helper, tmp_path, tmp_path / "state", volume, "owner")
    assert not (volume / "filler.bin").exists()
    assert not json.loads((tmp_path / "state/result.json").read_bytes())["qualified"]


@pytest.mark.workflow
def test_checkpoint_volume_artifacts_exclude_retained_payloads():
    path = Path(__file__).parents[1] / ".github/workflows/tests.yml"
    job = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["mxc-files"]
    upload = next(
        s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")
    )
    paths = [p for p in upload["with"]["path"].splitlines() if "checkpoint-volume/" in p]
    assert len(paths) == 3
    assert all(p.endswith((".json", ".log")) and "**" not in p for p in paths)
