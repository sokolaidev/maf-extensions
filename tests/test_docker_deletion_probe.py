"""Qualification observations must distinguish absence from a failed observer."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "docker_deletion_probe",
    Path(__file__).resolve().parents[1] / "scripts/experiments/docker_deletion_probe.py",
)
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


@pytest.mark.parametrize("exists", [False, True])
def test_file_observation_executes_the_guest_check(monkeypatch, tmp_path, exists):
    target = tmp_path / "target"
    if exists:
        target.write_text("sentinel")
    original_run = subprocess.run

    def local_guest(args, **kwargs):
        assert args[:5] == ["docker", "exec", "--user", "0", "fixture"]
        return original_run([sys.executable, "-c", args[-1]], **kwargs)

    monkeypatch.setattr(probe.subprocess, "run", local_guest)
    assert probe._exists("fixture", str(target)) is exists


@pytest.mark.parametrize("observer", ["file", "container"])
@pytest.mark.parametrize("exit_code", [1, 125, 126, 127])
def test_failed_observation_cannot_report_absence(monkeypatch, observer, exit_code):
    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, exit_code, "", "observer unavailable")

    monkeypatch.setattr(probe.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="observer unavailable"):
        if observer == "file":
            probe._exists("fixture", "/target")
        else:
            probe._container_present("physical-id")


@pytest.mark.parametrize("output", ["", "null", "0", "{}", '"false"'])
def test_file_observation_requires_a_boolean(monkeypatch, output):
    def malformed(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(probe.subprocess, "run", malformed)
    with pytest.raises(ValueError):
        probe._exists("fixture", "/target")


def test_guest_stat_error_is_not_absence(monkeypatch):
    original_run = subprocess.run

    def denied_guest(args, **kwargs):
        deny = "import os\ndef denied(path): raise PermissionError('denied')\nos.lstat=denied\n"
        return original_run([sys.executable, "-c", deny + args[-1]], **kwargs)

    monkeypatch.setattr(probe.subprocess, "run", denied_guest)
    with pytest.raises(RuntimeError, match="PermissionError"):
        probe._exists("fixture", "/target")
