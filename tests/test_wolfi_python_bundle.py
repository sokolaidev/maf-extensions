"""Shared CI preparation must preserve source identity and transferred APK bytes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_wolfi_python as builder  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.fixture
def bundle(tmp_path):
    for name in (
        "maf-python.rsa.pub",
        "python-3.13.yaml",
        "x86_64/APKINDEX.tar.gz",
        *sorted(builder.required_apks()),
    ):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(name.encode())
    builder.seal_bundle(tmp_path)
    return tmp_path


def test_valid_shared_bundle_skips_compilation_and_reaches_consumer(bundle, monkeypatch):
    commands = []
    monkeypatch.setenv("MAF_WOLFI_PYTHON_PACKAGES", str(bundle))
    monkeypatch.setattr(
        builder, "build_packages", lambda destination: pytest.fail("compiled again")
    )
    monkeypatch.setattr(builder.subprocess, "run", lambda command, **kw: commands.append(command))
    builder.build_image(["docker", "build", "images/shell-sandbox"])
    assert len(commands) == 1
    assert f"python-packages={bundle}" in commands[0]


@pytest.mark.parametrize(
    "mutation",
    ["apk", "missing", "extra", "private-key", "input", "platform", "stale", "future", "manifest"],
)
def test_invalid_bundle_refuses_before_running_docker(bundle, monkeypatch, mutation):
    record = json.loads((bundle / "bundle.json").read_text())
    if mutation == "apk":
        (bundle / sorted(builder.required_apks())[0]).write_bytes(b"changed")
    elif mutation == "missing":
        (bundle / "maf-python.rsa.pub").unlink()
    elif mutation in {"extra", "private-key"}:
        (bundle / ("extra.txt" if mutation == "extra" else "maf-python.rsa")).write_text("refuse")
    elif mutation == "manifest":
        (bundle / "bundle.json").unlink()
    else:
        field, value = {
            "input": ("inputs", "wrong"),
            "platform": ("platform", "linux/arm64"),
            "stale": ("createdAt", "2000-01-01T00:00:00+00:00"),
            "future": ("createdAt", "2100-01-01T00:00:00+00:00"),
        }[mutation]
        record[field] = value
        (bundle / "bundle.json").write_text(json.dumps(record))
    monkeypatch.setenv("MAF_WOLFI_PYTHON_PACKAGES", str(bundle))
    monkeypatch.setattr(
        builder.subprocess, "run", lambda *args, **kw: pytest.fail("executed Docker")
    )
    with pytest.raises((ValueError, OSError)):
        builder.build_image(["docker", "build", "images/shell-sandbox"])


def test_input_key_covers_recipe_and_builder(monkeypatch, tmp_path):
    helper = tmp_path / "scripts/build_wolfi_python.py"
    helper.parent.mkdir()
    helper.write_text("builder")
    context = tmp_path / "images/wolfi-python"
    context.mkdir(parents=True)
    for name in ("Dockerfile", "configure.py", "build.sh"):
        (context / name).write_text(name)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "__file__", str(helper))
    original = builder.bundle_key()
    for path in (helper, context / "Dockerfile", context / "configure.py", context / "build.sh"):
        previous = path.read_text()
        path.write_text(previous + "changed")
        assert builder.bundle_key() != original
        path.write_text(previous)
    (context / "additional-input").write_text("future recipe input")
    assert builder.bundle_key() != original
    (context / "Dockerfile").unlink()
    with pytest.raises(ValueError, match="Missing Python build input"):
        builder.bundle_key()


@pytest.mark.parametrize("version", ["3.13.18-r0", "3.13.18_git20261030-r0"])
def test_bundle_requires_the_apks_named_by_consumers(
    monkeypatch, tmp_path, tmp_path_factory, version
):
    source_root = tmp_path_factory.mktemp("source")
    required = {
        f"x86_64/python-3.13-{version}.apk",
        f"x86_64/python-3.13-base-{version}.apk",
    }
    for context in ("shell", "document"):
        dockerfile = source_root / "images" / context / "Dockerfile"
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text("RUN apk add " + " ".join(f"/python-packages/{n}" for n in required))
    monkeypatch.setattr(builder, "ROOT", source_root)
    for name in ("maf-python.rsa.pub", "python-3.13.yaml", "x86_64/APKINDEX.tar.gz"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"output")
    for name in required:
        (tmp_path / name.replace(version, "0.0-r0")).write_bytes(b"obsolete APK")
    with pytest.raises(ValueError, match="incomplete"):
        builder.bundle_files(tmp_path)
    for name in required:
        (tmp_path / name).write_bytes(b"required APK")
    assert required <= builder.bundle_files(tmp_path).keys()


@pytest.mark.parametrize("defect", ["missing", "different-consumer", "different-base", "variable"])
def test_ambiguous_consumer_apks_are_rejected(monkeypatch, tmp_path, defect):
    for context in ("shell", "document"):
        dockerfile = tmp_path / "images" / context / "Dockerfile"
        dockerfile.parent.mkdir(parents=True)
        version = "2.0-r0" if defect == "different-consumer" and context == "document" else "1.0-r0"
        base = "2.0-r0" if defect == "different-base" else version
        source = (
            f"RUN apk add /python-packages/x86_64/python-3.13-{version}.apk "
            f"/python-packages/x86_64/python-3.13-base-{base}.apk"
        )
        if defect == "missing":
            source = "FROM scratch"
        elif defect == "variable":
            source = source.replace(version, "${PYTHON_VERSION}")
        dockerfile.write_text(source)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="APK pair"):
        builder.required_apks()
