"""Image verification binds the smoke result to retained, immutable build inputs."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_hyperlight_aks_image as builder
import hyperlight_image_smoke as smoke

IMAGE_ID = "sha256:" + "1" * 64


@pytest.fixture
def payload(tmp_path, monkeypatch):
    (tmp_path / "wheels").mkdir()
    for name in smoke.PACKAGES:
        with ZipFile(tmp_path / "wheels" / f"{name}-1-py3-none-any.whl", "w") as wheel:
            wheel.writestr(f"{name}.dist-info/METADATA", f"Name: {name}\nVersion: 1\n")
    for name in (
        "Dockerfile",
        ".dockerignore",
        "requirements.txt",
        "hyperlight-probe.py",
        "verify.py",
    ):
        (tmp_path / name).write_text(name)
    (tmp_path / "source.json").write_text('{"revision":"abc","dirty":true}')
    inputs = {}
    for path in tmp_path.rglob("*"):
        if path.is_file():
            name = path.relative_to(tmp_path).as_posix()
            inputs["probe.py" if name == "hyperlight-probe.py" else name] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    (tmp_path / "build-inputs.json").write_text(json.dumps(inputs))
    monkeypatch.setattr(smoke, "version", lambda name: "1")
    return tmp_path


def test_payload_records_installed_workspace_and_exact_manifest(payload):
    result = smoke.verify_payload(payload)
    assert result["workspace_packages"] == dict.fromkeys(smoke.PACKAGES, "1")
    assert result["source"] == {"revision": "abc", "dirty": True}
    assert (
        result["build_inputs_sha256"]
        == hashlib.sha256((payload / "build-inputs.json").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "name", ["requirements.txt", "source.json", "hyperlight-probe.py", "verify.py"]
)
def test_payload_refuses_modified_inputs(payload, name):
    (payload / name).write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        smoke.verify_payload(payload)


def test_payload_refuses_missing_input_even_if_manifest_omits_it(payload):
    path = payload / "build-inputs.json"
    inputs = json.loads(path.read_text())
    del inputs["probe.py"]
    path.write_text(json.dumps(inputs))
    with pytest.raises(ValueError, match="unexpected build inputs"):
        smoke.verify_payload(payload)


def test_payload_refuses_installed_version_different_from_wheel(payload, monkeypatch):
    monkeypatch.setattr(smoke, "version", lambda name: "2")
    with pytest.raises(ValueError, match="does not match wheel"):
        smoke.verify_payload(payload)


def test_source_record_rejects_dirty_release_and_does_not_export_local_paths(tmp_path, monkeypatch):
    (tmp_path / "uv.lock").write_text("locked")
    monkeypatch.setattr(builder, "ROOT", tmp_path)

    def execute(command, **kwargs):
        output = "a" * 40 if command[1] == "rev-parse" else " M private-local-file.txt\n"
        return subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr(subprocess, "run", execute)
    assert builder.source_record() == {
        "repository": builder.SOURCE_URL,
        "revision": "a" * 40,
        "dirty": True,
        "uv_lock_sha256": hashlib.sha256(b"locked").hexdigest(),
    }
    with pytest.raises(ValueError, match="clean source"):
        builder.source_record(require_clean=True)


@pytest.mark.parametrize(
    "failure", [None, "smoke", "digest", "root", "platform", "command", "entrypoint"]
)
def test_build_verifies_exact_image_and_never_keeps_stale_success(tmp_path, monkeypatch, failure):
    commands = []
    (tmp_path / "build-inputs.json").write_text("{}")
    record = tmp_path / "image-verification.json"
    record.write_text("old success")

    def execute(command, **kwargs):
        commands.append(command)
        if command[1] == "build":
            assert not record.exists()
            Path(command[command.index("--iidfile") + 1]).write_text(IMAGE_ID)
            return subprocess.CompletedProcess(command, 0)
        if command[1] == "image":
            assert command[-1] == IMAGE_ID
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Os": "linux",
                            "Architecture": "arm64" if failure == "platform" else "amd64",
                            "Config": {
                                "User": "0" if failure == "root" else "65534:65534",
                                "Cmd": [] if failure == "command" else builder.PROBE_COMMAND,
                                "Entrypoint": ["sh"] if failure == "entrypoint" else None,
                            },
                        }
                    ]
                ),
            )
        assert command[1] == "run"
        if failure == "smoke":
            raise subprocess.CalledProcessError(1, command, stderr="invalid payload")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "build_inputs_sha256": "wrong"
                    if failure == "digest"
                    else hashlib.sha256(b"{}").hexdigest(),
                    "hypervisor_execution_verified": False,
                }
            ),
        )

    monkeypatch.setattr(subprocess, "run", execute)
    if failure:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            builder.build_and_verify(tmp_path, "mutable:tag")
        assert not record.exists()
        return
    result = builder.build_and_verify(tmp_path, "mutable:tag")
    assert json.loads(record.read_bytes()) == result
    assert result["local_image_id"] == IMAGE_ID
    assert result["registry_digest"] is None
    assert result["signed_provenance_verified"] is False
    command = commands[-1]
    assert command[-4:] == [IMAGE_ID, "-I", "-B", "/opt/verify.py"]
    assert "mutable:tag" not in command
    for option, value in (
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
    ):
        assert command[command.index(option) + 1] == value
    assert "--read-only" in command
    assert "--device" not in command
    assert "--privileged" not in command


def test_dirty_rebuild_removes_previous_success_before_refusing_source(tmp_path, monkeypatch):
    record = tmp_path / "image-verification.json"
    record.write_text("old success")

    def refuse(**kwargs):
        raise ValueError("release image requires a clean source checkout")

    monkeypatch.setattr(builder, "source_record", refuse)
    with pytest.raises(ValueError, match="clean source"):
        builder.prepare(tmp_path, require_clean=True)
    assert not record.exists()
