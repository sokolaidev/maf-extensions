"""Manifest-driven engine installation and image version metadata."""

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

IMAGE_SOURCE = Path(__file__).resolve().parents[1] / "images/terraform-sandbox"
sys.path.insert(0, str(IMAGE_SOURCE))
import build_image  # noqa: E402
import install  # noqa: E402


@pytest.fixture
def config_path(tmp_path):
    for name in ("build.json", "dependencies.terraform.json", "dependencies.opentofu.json"):
        (tmp_path / name).write_bytes((IMAGE_SOURCE / name).read_bytes())
    return tmp_path / "build.json"


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_profiles_reuse_approved_provider_manifests(engine):
    empty = install.load_plan(engine, "builtin")
    assert empty["providers"] == []
    approved = json.loads((IMAGE_SOURCE / f"dependencies.{engine}.json").read_text())
    mirrored = install.load_plan(engine, "random")
    assert mirrored["providers"] == approved["providers"]


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_changed_json_pin_drives_download_metadata_and_build_tag(
    config_path, tmp_path, monkeypatch, engine
):
    config = json.loads(config_path.read_text())
    selected = config["engines"][engine]
    selected["version"] = "9.8.7"
    selected["url"] = f"https://example.com/{engine}/9.8.7/engine.zip"
    binary = b"test binary bytes, never executed on the host"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(selected["executable"], binary)
        archive.writestr("LICENSE.txt", "test license")
    payload = output.getvalue()
    selected["sha256"] = hashlib.sha256(payload).hexdigest()
    config_path.write_text(json.dumps(config))
    downloads = []
    executions = []

    def download(url, digest):
        downloads.append((url, digest))
        return payload

    def report_version(command, **kwargs):
        executions.append(command)
        assert kwargs["env"]["CHECKPOINT_DISABLE"] == "1"
        assert "TF_CLI_ARGS" not in kwargs["env"]
        return SimpleNamespace(stdout=b'{"terraform_version":"9.8.7"}')

    monkeypatch.setattr(install, "download", download)
    monkeypatch.setattr(install.subprocess, "run", report_version)
    destination, binaries = tmp_path / "image", tmp_path / "bin"
    binaries.mkdir()
    install.main(
        engine,
        "builtin",
        "9.8.7",
        config_path=config_path,
        destination=destination,
        bin_directory=binaries,
    )
    assert downloads == [(selected["url"], selected["sha256"])]
    assert executions == [[str(binaries / selected["executable"]), "version", "-json"]]
    metadata = json.loads((destination / "engine.json").read_text())
    assert metadata == {
        "engine": engine,
        "version": "9.8.7",
        "executable": selected["executable"],
        "platform": "linux/amd64",
        "archive_sha256": selected["sha256"],
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
        "profile": "builtin",
    }
    monkeypatch.setattr(
        build_image,
        "load_plan",
        lambda engine, profile, _: install.load_plan(engine, profile, config_path),
    )
    command = build_image.build_command(engine, "builtin")
    assert "ENGINE_VERSION=9.8.7" in command
    assert command[command.index("--tag") + 1] == f"maf-{engine}:9.8.7-builtin"
    assert "BASE_IMAGE=" + config["base_image"] in command
    overridden = build_image.build_command(engine, "builtin", "custom:ci")
    assert "ENGINE_VERSION=9.8.7" in overridden and "custom:ci" in overridden


def test_mismatched_label_version_refuses_before_download(config_path, monkeypatch):
    monkeypatch.setattr(install, "download", lambda *a: pytest.fail("download must not start"))
    with pytest.raises(ValueError, match="metadata must match"):
        install.main("terraform", "builtin", "wrong", config_path=config_path)


def test_actual_binary_version_must_match_manifest(config_path, tmp_path, monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("terraform", b"fake")
    monkeypatch.setattr(install, "download", lambda *a: payload.getvalue())
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=b'{"terraform_version":"0.0.1"}'),
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    version = install.load_plan("terraform", "builtin", config_path)["version"]
    with pytest.raises(ValueError, match="downloaded binary version"):
        install.main(
            "terraform",
            "builtin",
            version,
            config_path=config_path,
            destination=tmp_path / "image",
            bin_directory=binaries,
        )
    assert not (tmp_path / "image/engine.json").exists()


@pytest.mark.parametrize("change", ["base", "platform", "executable", "digest", "profile-path"])
def test_invalid_build_configuration_is_refused(config_path, change):
    config = json.loads(config_path.read_text())
    engine = config["engines"]["terraform"]
    if change == "base":
        config["base_image"] = "python:latest"
    elif change == "platform":
        config["platform"] = "linux/arm64"
    elif change == "executable":
        engine["executable"] = "../../other"
    elif change == "digest":
        engine["sha256"] = "missing"
    else:
        engine["profiles"]["builtin"] = "../other.json"
    config_path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        install.load_plan("terraform", "builtin", config_path)


def test_wrong_engine_provider_profile_is_refused(config_path):
    config = json.loads(config_path.read_text())
    config["engines"]["terraform"]["profiles"]["random"] = "dependencies.opentofu.json"
    config_path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="manifest engine mismatch"):
        install.load_plan("terraform", "random", config_path)


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_live_image_labels_match_installed_binary_and_runtime_metadata(engine):
    image = os.environ.get(f"MAF_{engine.upper()}_E2E_IMAGE")
    if not image:
        pytest.skip("needs explicit real engine image")
    inspected = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    labels = json.loads(inspected.stdout)[0]["Config"]["Labels"]
    script = (
        "import json,pathlib,subprocess; "
        "p=pathlib.Path('/opt/maf-terraform'); m=json.loads((p/'engine.json').read_text()); "
        "r=subprocess.run(['/usr/local/bin/'+m['executable'],'version','-json'],capture_output=True,check=True); "
        "assert json.loads(r.stdout)['terraform_version']==m['version']; print(json.dumps(m))"
    )
    checked = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", image, "python3", "-I", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    metadata = json.loads(checked.stdout)
    assert labels["ai.sokol.maf.engine"] == metadata["engine"] == engine
    assert labels["ai.sokol.maf.engine.version"] == metadata["version"]
    assert labels["org.opencontainers.image.version"] == metadata["version"]
    assert metadata["platform"] == "linux/amd64"
