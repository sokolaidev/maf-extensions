"""Reject source and dependency drift before producing a Terraform engine."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "images/terraform-sandbox"
sys.path.insert(0, str(SOURCE))
import build_go  # noqa: E402
import build_opentofu  # noqa: E402
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


@pytest.mark.parametrize("failures", [0, 1, 2])
@pytest.mark.parametrize("stage", ["get", "download"])
def test_module_download_recovers_with_bounded_backoff(
    tmp_path, monkeypatch, capsys, stage, failures
):
    calls, delays = [], []
    get = ["go", "get", "example.com/module@v1.2.3"]
    download = ["go", "mod", "download"]
    target = get if stage == "get" else download

    def run(command, **kwargs):
        assert kwargs == {"cwd": tmp_path, "check": True}
        calls.append(command)
        if command == target and calls.count(command) <= failures:
            raise subprocess.CalledProcessError(1, command, stderr="INTERNAL_ERROR")

    monkeypatch.setattr(build_go.subprocess, "run", run)
    monkeypatch.setattr(build_go.time, "sleep", delays.append)
    build_go.download_modules(tmp_path, {"example.com/module": "v1.2.3"})
    assert calls == (
        [get] * (failures + 1) + [download]
        if stage == "get"
        else [get] + [download] * (failures + 1)
    )
    assert delays == [2, 4][:failures]
    assert capsys.readouterr().err.count("retrying") == failures


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize("stage", ["get", "download", "verify", "test"])
def test_source_build_stops_after_download_exhaustion_or_first_check_failure(
    tmp_path, monkeypatch, engine, stage
):
    builder = build_terraform if engine == "terraform" else build_opentofu
    plan = json.loads((SOURCE / "image.json").read_text(encoding="utf-8"))["engines"][engine]
    source = plan["source_build"]
    calls, delays = [], []
    failure = None
    prefixes = {
        "get": ["go", "get"],
        "download": ["go", "mod", "download"],
        "verify": ["go", "mod", "verify"],
        "test": ["go", "test"],
    }
    target = prefixes[stage]

    def run(command, **kwargs):
        nonlocal failure
        calls.append(command)
        if command[: len(target)] == target:
            failure = subprocess.CalledProcessError(1, command)
            raise failure

    def output(command, **kwargs):
        if command == ["go", "env", "GOVERSION"]:
            return source["go_version"]
        if command == ["git", "rev-parse", "HEAD"]:
            return source["revision"]
        assert command[:3] == ["go", "list", "-m"]
        version = source["modules"][command[-1]]
        return json.dumps({"Version": version}) if engine == "terraform" else version

    monkeypatch.setattr(builder.subprocess, "run", run)
    monkeypatch.setattr(builder.subprocess, "check_output", output)
    monkeypatch.setattr(build_go.time, "sleep", delays.append)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        if engine == "terraform":
            build_terraform.build(plan, tmp_path / "checkout", tmp_path / "output")
        else:
            build_opentofu.main()
    assert caught.value is failure
    assert sum(command[: len(target)] == target for command in calls) == (
        3 if stage in {"get", "download"} else 1
    )
    assert delays == ([2, 4] if stage in {"get", "download"} else [])
    assert calls[-1][: len(target)] == target
    if stage in {"verify", "test"}:
        assert calls.index(["go", "mod", "download"]) < calls.index(["go", "mod", "verify"])
    assert not (tmp_path / "output").exists()
