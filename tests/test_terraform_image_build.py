"""Manifest-driven engine installation and image version metadata."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import uuid
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
    for name in ("image.json", "dependencies.terraform.json", "dependencies.opentofu.json"):
        (tmp_path / name).write_bytes((IMAGE_SOURCE / name).read_bytes())
    return tmp_path / "image.json"


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_profiles_reuse_approved_provider_manifests(engine):
    empty = install.load_plan(engine, "builtin")
    assert empty["providers"] == []
    approved = json.loads((IMAGE_SOURCE / f"dependencies.{engine}.json").read_text())
    mirrored = install.load_plan(engine, "random")
    assert mirrored["providers"] == approved["providers"]


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize(
    "source", ["example.com/foo_bar/random", "example.com/hashicorp/random_type"]
)
def test_provider_source_uses_preparation_manifest_grammar(config_path, engine, source):
    path = config_path.with_name(f"dependencies.{engine}.json")
    manifest = json.loads(path.read_text())
    manifest["providers"][0]["source"] = source
    path.write_text(json.dumps(manifest))
    assert install.load_plan(engine, "random", config_path)["providers"][0]["source"] == source


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize("version", ["3.7.2-rc1", "3.7.2-beta.2"])
def test_provider_prerelease_uses_preparation_manifest_grammar(config_path, engine, version):
    path = config_path.with_name(f"dependencies.{engine}.json")
    manifest = json.loads(path.read_text())
    manifest["providers"][0]["version"] = version
    path.write_text(json.dumps(manifest))
    import terraform_dependencies as prep

    assert install.load_plan(engine, "random", config_path)["providers"][0]["version"] == version
    assert prep.checked_manifest(manifest)["providers"][0]["version"] == version


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize("target", ["image", "provider"])
@pytest.mark.parametrize("schema", [True, 1.0])
def test_schema_requires_integer_one_before_download(
    config_path, monkeypatch, engine, target, schema
):
    path = (
        config_path if target == "image" else config_path.with_name(f"dependencies.{engine}.json")
    )
    document = json.loads(path.read_text())
    document["schema"] = schema
    path.write_text(json.dumps(document))
    monkeypatch.setattr(install, "download", lambda *a: pytest.fail("download must not start"))
    version = install.load_plan(engine, "builtin")["version"]
    with pytest.raises(ValueError):
        install.main(
            engine,
            "random",
            version,
            config_path=config_path,
            destination=config_path.parent / "output",
        )


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize("changed_digest", [False, True])
def test_repeated_provider_identity_refused_before_download(
    config_path, monkeypatch, engine, changed_digest
):
    path = config_path.with_name(f"dependencies.{engine}.json")
    manifest = json.loads(path.read_text())
    repeated = json.loads(json.dumps(manifest["providers"][0]))
    if changed_digest:
        repeated["artifact"]["sha256"] = "0" * 64
    manifest["providers"].append(repeated)
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(install, "download", lambda *a: pytest.fail("download must not start"))
    version = json.loads(config_path.read_text())["engines"][engine]["version"]
    with pytest.raises(ValueError, match="duplicate provider identity"):
        install.main(
            engine,
            "random",
            version,
            config_path=config_path,
            destination=config_path.parent / "output",
        )


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize("target", ["image", "provider", "artifact"])
def test_ambiguous_image_inputs_refused_before_download(config_path, monkeypatch, engine, target):
    if target == "image":
        text = config_path.read_text()
        config_path.write_text('{"engines":{},' + text.lstrip()[1:])
    else:
        path = config_path.with_name(f"dependencies.{engine}.json")
        text = path.read_text()
        if target == "provider":
            text = '{"providers":[],' + text.lstrip()[1:]
        else:
            text = text.replace('"artifact": {', '"artifact": {"sha256":"' + "0" * 64 + '",', 1)
        path.write_text(text)
    monkeypatch.setattr(install, "download", lambda *a: pytest.fail("download must not start"))
    version = install.load_plan(engine, "builtin")["version"]
    with pytest.raises(ValueError, match="duplicate JSON key"):
        install.main(
            engine,
            "random",
            version,
            config_path=config_path,
            destination=config_path.parent / "output",
        )


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_build_includes_configured_provider_manifest(config_path, monkeypatch, engine):
    config = json.loads(config_path.read_text())
    config["engines"][engine]["profiles"]["custom"] = "approved.custom.json"
    config_path.write_text(json.dumps(config))
    config_path.with_name("approved.custom.json").write_bytes(
        (IMAGE_SOURCE / f"dependencies.{engine}.json").read_bytes()
    )
    monkeypatch.setattr(build_image, "__file__", str(config_path.with_name("build_image.py")))
    command = build_image.build_command(engine, "custom")
    assert "PROVIDER_MANIFEST=approved.custom.json" in command
    assert "PROVIDER_MANIFEST=image.json" in build_image.build_command(engine, "builtin")


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_live_build_with_custom_provider_manifest(tmp_path, engine):
    if os.environ.get("MAF_IMAGE_BUILD_TESTS") != "1":
        pytest.skip("needs explicit Docker image build opt-in")
    context = tmp_path / "context"
    context.mkdir()
    for name in ("Dockerfile", "build_image.py", "install.py", "runner.py", "image.json"):
        shutil.copyfile(IMAGE_SOURCE / name, context / name)
    manifest_name = "approved.custom.json"
    shutil.copyfile(IMAGE_SOURCE / f"dependencies.{engine}.json", context / manifest_name)
    config_path = context / "image.json"
    config = json.loads(config_path.read_text())
    config["engines"][engine]["profiles"]["custom"] = manifest_name
    config_path.write_text(json.dumps(config))
    tag = "maf-image-config-test:" + uuid.uuid4().hex
    try:
        built = subprocess.run(
            [
                sys.executable,
                str(context / "build_image.py"),
                "--engine",
                engine,
                "--profile",
                "custom",
                "--tag",
                tag,
            ],
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        inspection_script = (
            "import json,pathlib; p=pathlib.Path('/opt/maf-terraform'); "
            "assert json.loads((p/'engine.json').read_text())['profile']=='custom'; "
            "assert len(list((p/'mirror').rglob('*.zip')))==1"
        )
        inspected = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                tag,
                "python3",
                "-I",
                "-c",
                inspection_script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    finally:
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=30)


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


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize(
    "version", ["3.7.2-", "3.7.2-.rc", "3.7.2-rc.", "3.7.2-rc..1", "3.7.2-RC1"]
)
def test_provider_invalid_prerelease_refuses_before_download(
    config_path, monkeypatch, engine, version
):
    import terraform_dependencies as prep

    path = config_path.with_name(f"dependencies.{engine}.json")
    policy = json.loads(path.read_text())
    policy["providers"][0]["version"] = version
    path.write_text(json.dumps(policy))
    monkeypatch.setattr(install, "download", lambda *a: pytest.fail("download must not start"))
    with pytest.raises(ValueError):
        install.main(
            engine,
            "random",
            install.load_plan(engine, "builtin")["version"],
            config_path=config_path,
        )
    with pytest.raises(prep.Refused, match="provider-version"):
        prep.checked_manifest(policy)
