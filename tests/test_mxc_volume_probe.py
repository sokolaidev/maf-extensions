"""Guard the disposable volume boundary and fail closed on missing evidence."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    probe = importlib.import_module("scripts.experiments.mxc_session_patch.volume_probe")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


def test_volume_refuses_wrong_owner_and_host_filesystem(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / ".mxc-volume-probe").write_text("owner")
    with pytest.raises(ValueError, match="ownership"):
        probe.validate_volume(volume, tmp_path, "other")
    monkeypatch.setattr(
        probe.shutil, "disk_usage", lambda _: SimpleNamespace(total=128 * probe.MIB)
    )
    with pytest.raises(ValueError, match="separate"):
        probe.validate_volume(volume, tmp_path, "owner")


@pytest.mark.parametrize("capacity", [63 * probe.MIB, 257 * probe.MIB])
def test_volume_refuses_unbounded_capacity(tmp_path, monkeypatch, capacity):
    (tmp_path / ".mxc-volume-probe").write_text("owner")
    monkeypatch.setattr(probe.shutil, "disk_usage", lambda _: SimpleNamespace(total=capacity))
    with pytest.raises(ValueError, match="capacity"):
        probe.validate_volume(tmp_path, tmp_path, "owner")


def test_existing_filler_is_never_overwritten(tmp_path):
    filler = tmp_path / "filler.bin"
    filler.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        probe.fill(tmp_path)
    assert filler.read_bytes() == b"existing"


def test_worker_failure_or_missing_report_refuses_qualification(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="overflow failed"):
        probe.launch(tmp_path, tmp_path, "token", "overflow")
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(FileNotFoundError):
        probe.launch(tmp_path, tmp_path, "token", "overflow")


@pytest.mark.workflow
def test_both_filesystems_require_independent_cleanup_and_retain_only_reports():
    path = Path(__file__).parents[1] / ".github/workflows/mxc-volume.yml"
    job = yaml.safe_load(path.read_text())["jobs"]["volume"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-24.04", "windows-latest"]
    cleanup = [step for step in job["steps"] if step.get("name", "").startswith("Detach")]
    assert len(cleanup) == 2
    assert all("always()" in step["if"] for step in cleanup)
    assert 'losetup -j "$image"' in cleanup[0]["run"]
    assert "(Get-DiskImage -ImagePath $image).Attached" in cleanup[1]["run"]
    upload = job["steps"][-1]
    assert upload["if"] == "always()"
    assert all(path.endswith(("*.json", "*.log")) for path in upload["with"]["path"].splitlines())


@pytest.mark.parametrize("error_number", [5, 13])
def test_filler_does_not_treat_other_io_errors_as_exhaustion(tmp_path, monkeypatch, error_number):
    class BrokenStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, data):
            raise OSError(error_number, "not disk full")

    monkeypatch.setattr(Path, "open", lambda *a, **kw: BrokenStream())
    with pytest.raises(OSError) as failed:
        probe.fill(tmp_path)
    assert failed.value.errno == error_number
