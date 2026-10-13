"""Fail-closed evidence checks for installed-worker qualification."""

import importlib
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
checker = importlib.import_module("openclaw_installed_worker_check")


def test_wheel_refuses_missing_runtime_modules(tmp_path):
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("maf_sandbox_mcp/__init__.py", "")
    with pytest.raises(RuntimeError, match="Incomplete runtime"):
        checker.verify_wheel(wheel)


def test_wheel_refuses_changed_installed_bytes(tmp_path):
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in ("__init__.py", "server.py", "workload_http.py", "workload_service.py"):
            archive.writestr("maf_sandbox_mcp/" + name, "wrong candidate")
    with pytest.raises(RuntimeError, match="differs from wheel"):
        checker.verify_wheel(wheel)


@pytest.mark.parametrize(
    "name,direct",
    [
        ("maf-sandbox-mcp", {"dir_info": {"editable": True}}),
        ("maf-sandbox", {"archive_info": {}}),
    ],
)
def test_environment_refuses_source_candidate_and_local_dependencies(monkeypatch, name, direct):
    dist = SimpleNamespace(
        metadata={"Name": name}, version="0.0.0", read_text=lambda _: json.dumps(direct)
    )
    monkeypatch.setattr(checker.importlib.metadata, "distributions", lambda: [dist])
    with pytest.raises(RuntimeError, match="Non-wheel candidate or local dependency"):
        checker.installed_environment()


def test_environment_refuses_changed_baseline(monkeypatch):
    monkeypatch.setattr(checker.importlib.metadata, "distributions", lambda: [])
    with pytest.raises(RuntimeError, match="baseline"):
        checker.installed_environment()
