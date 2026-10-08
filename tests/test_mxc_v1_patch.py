"""Exercise reversible MXC 1.0 layers and keep dependency resolution distinct from qualification."""

from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    patch = importlib.import_module("scripts.experiments.mxc_v1_patch.patch")
    runner = importlib.import_module("scripts.experiments.mxc_v1_patch.qualification_runner")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


@pytest.fixture
def layers(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    sources = {name: tmp_path / name for name in ("session", "runtime", "host")}
    pins = {"layers": {}}
    for name, source in sources.items():
        source.mkdir()
        (source / "code").write_text("baseline\n", encoding="utf-8")
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
            subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
        pins["mxc_base" if name == "session" else f"{name}_base"] = patch.git(
            source, "rev-parse", "HEAD"
        ).strip()
    values = {name: "baseline\n" for name in sources}
    for name, targets in (
        ("session", ("session",)),
        ("output", ("session", "runtime")),
        ("storage", tuple(sources)),
    ):
        pins["layers"][name] = {}
        for target in targets:
            before, after = values[target], name + "\n"
            text = "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    after.splitlines(True),
                    fromfile="a/code",
                    tofile="b/code",
                )
            )
            (assets / f"{name}-{target}.patch").write_text(text, encoding="utf-8", newline="\n")
            pins["layers"][name][target] = {
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "files": {
                    "code": {
                        "before": hashlib.sha256(before.encode()).hexdigest(),
                        "after": hashlib.sha256(after.encode()).hexdigest(),
                    }
                },
            }
            values[target] = after
    (assets / "manifest.json").write_text(json.dumps(pins), encoding="utf-8")
    monkeypatch.setattr(patch, "ROOT", assets)
    return sources


def test_layers_apply_and_remove_without_changing_the_pristine_sources(layers):
    for layer in ("session", "output", "storage"):
        patch.overlay("apply", layer, layers)
        patch.overlay("check", layer, layers)
    for layer in ("storage", "output", "session"):
        patch.overlay("remove", layer, layers)
        patch.overlay("check", layer, layers)
    for source in layers.values():
        assert patch.git(source, "status", "--porcelain") == ""


@pytest.mark.parametrize("target", ["session", "runtime", "host"])
def test_drift_in_any_prerequisite_refuses_before_any_checkout_is_modified(layers, target):
    patch.overlay("apply", "session", layers)
    (layers[target] / "code").write_text("changed\n", encoding="utf-8")
    before = {name: (source / "code").read_bytes() for name, source in layers.items()}
    with pytest.raises(ValueError, match="modified or mixed"):
        patch.overlay("apply", "output", layers)
    assert before == {name: (source / "code").read_bytes() for name, source in layers.items()}


def test_mixed_layers_and_wrong_order_refuse(layers):
    with pytest.raises(ValueError, match="order"):
        patch.overlay("apply", "storage", layers)
    patch.overlay("apply", "session", layers)
    patch.overlay("apply", "output", layers)
    with pytest.raises(ValueError, match="order"):
        patch.overlay("remove", "session", layers)


def test_patch_checksum_refuses_before_applying(layers):
    path = patch.ROOT / "session-session.patch"
    path.write_bytes(path.read_bytes() + b"changed\n")
    with pytest.raises(ValueError, match="checksum"):
        patch.overlay("apply", "session", layers)
    assert all((source / "code").read_text() == "baseline\n" for source in layers.values())


def test_dependency_resolution_cannot_be_reported_as_native_qualification(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(runner, "sources", lambda *args: [])
    monkeypatch.setattr(runner, "run", lambda *args, **kwargs: commands.append(args))
    report = {"status": "unqualified"}
    runner.qualify(tmp_path, report, "resolve-lock")
    assert report["status"] == "dependencies-resolved"
    assert "helper_sha256" not in report
    assert not any("cargo-build" in command for command in commands)


@pytest.mark.workflow
def test_migration_workflow_is_opt_in_and_retains_lock_and_reports():
    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["mxc-v1"]
    assert "if" not in job
    assert job["strategy"]["fail-fast"] is False
    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-24.04", "windows-latest"}
    assert all("inputs.mxc_v1 != 'off'" in step["if"] for step in job["steps"])
    upload = next(step for step in job["steps"] if "upload-artifact" in step.get("uses", ""))
    assert "always()" in upload["if"]
    assert "build/Cargo.lock" in upload["with"]["path"]
    assert "**" not in upload["with"]["path"]
