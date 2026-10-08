"""Check reversible kernel edits and keep build evidence distinct from qualification."""

from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    patch = importlib.import_module("scripts.experiments.mxc_streams_patch.kernel_patch")
    builder = importlib.import_module("scripts.experiments.mxc_streams_patch.build_kernel")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    source = tmp_path / "runtime"
    assets = tmp_path / "assets"
    assets.mkdir()
    pins = {"submodules": {}, "files": {}}
    for relative in ("", "kernel/unikraft", "kernel/app-elfloader", "kernel/libs/libelf"):
        target = source / relative
        target.mkdir(parents=True, exist_ok=True)
        (target / "code").write_text("baseline\n", encoding="utf-8")
        for args in (
            ("init", "-q"),
            ("add", "code"),
            (
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "fixture",
            ),
        ):
            subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)
        commit = patch.git(target, "rev-parse", "HEAD").strip()
        if relative:
            pins["submodules"][relative] = commit
        else:
            pins["runtime_base"] = commit
    delta = ""
    for name, before, after in (("code", "baseline\n", "changed\n"), ("new", "", "new\n")):
        delta += "".join(
            difflib.unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                fromfile=f"a/{name}" if before else "/dev/null",
                tofile=f"b/{name}",
            )
        )
        pins["files"][name] = {
            "before": hashlib.sha256(before.encode()).hexdigest() if before else None,
            "after": hashlib.sha256(after.encode()).hexdigest(),
        }
    (assets / "kernel.patch").write_text(delta, encoding="utf-8", newline="\n")
    pins["patch_sha256"] = hashlib.sha256(delta.encode()).hexdigest()
    (assets / "kernel.json").write_text(json.dumps(pins), encoding="utf-8")
    monkeypatch.setattr(patch, "ROOT", assets)
    return source


def test_kernel_roundtrip_removes_new_file_and_restores_pristine_source(kernel):
    patch.overlay("apply", kernel)
    assert (kernel / "kernel/unikraft/new").read_text() == "new\n"
    patch.overlay("check", kernel)
    patch.overlay("remove", kernel)
    assert patch.git(kernel / "kernel/unikraft", "status", "--porcelain") == ""
    patch.overlay("check", kernel)


@pytest.mark.parametrize("fault", ["source", "patch", "mixed", "new-file"])
def test_kernel_refuses_drift_before_modifying_any_files(kernel, fault):
    target = kernel / "kernel/unikraft"
    if fault == "source":
        pins = patch.metadata()
        pins["submodules"]["kernel/libs/libelf"] = "0" * 40
        (patch.ROOT / "kernel.json").write_text(json.dumps(pins), encoding="utf-8")
    elif fault == "patch":
        with (patch.ROOT / "kernel.patch").open("ab") as output:
            output.write(b"changed\n")
    elif fault == "mixed":
        (target / "code").write_text("unrelated change\n", encoding="utf-8")
    else:
        (target / "new").write_text("user file\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        patch.overlay("apply", kernel)
    assert before == {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()}


def test_failed_build_retains_unqualified_report(tmp_path, monkeypatch):
    root = tmp_path / "build"
    monkeypatch.setattr(sys, "argv", ["build_kernel", "--root", str(root)])
    monkeypatch.setattr(sys, "platform", "linux")

    def fail(*args):
        raise subprocess.CalledProcessError(1, "docker")

    monkeypatch.setattr(builder, "build", fail)
    assert builder.main() == 1
    report = json.loads((root / "kernel-result.json").read_text())
    assert report["status"] == "unqualified"
    assert "kernel_sha256" not in report
    assert "docker" in report["reason"]
