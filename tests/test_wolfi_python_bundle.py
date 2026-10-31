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
        "x86_64/python-3.13-3.13.18-r0.apk",
        "x86_64/python-3.13-base-3.13.18-r0.apk",
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
        (bundle / "x86_64/python-3.13-3.13.18-r0.apk").write_bytes(b"changed")
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
