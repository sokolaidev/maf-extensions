"""Archive and inventory controls for transitive installed dependencies."""

from __future__ import annotations

import importlib
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
assets = importlib.import_module("openclaw_conformance.assets")
e = importlib.import_module("openclaw_conformance.evidence")


@pytest.mark.parametrize("name", ["dependency/runtime.py", "dependency/native.pyd"])
def test_wheel_dependency_bytes_must_match_archive(tmp_path, name):
    site = tmp_path / "worker/Lib/site-packages"
    target = site / name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    archive = tmp_path / "package.whl"
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr(name, b"original")
    assert assets.wheel(archive, site, tmp_path) == {target.relative_to(tmp_path).as_posix()}
    target.write_bytes(b"changed")
    with pytest.raises(e.Refused, match="locked_archive_bytes"):
        assets.wheel(archive, site, tmp_path)


@pytest.mark.parametrize("name", ["runtime.js", "native.node"])
def test_npm_transitive_bytes_must_match_archive(tmp_path, name):
    package = tmp_path / "openclaw/node_modules/dependency"
    package.mkdir(parents=True)
    target = package / name
    target.write_bytes(b"original")
    archive = tmp_path / "dependency.tgz"
    with tarfile.open(archive, "w:gz") as source:
        member = tarfile.TarInfo("package/" + name)
        member.size = 8
        source.addfile(member, io.BytesIO(b"original"))
    assert assets.npm(archive, package, tmp_path) == {target.relative_to(tmp_path).as_posix()}
    target.write_bytes(b"changed")
    with pytest.raises(e.Refused, match="locked_archive_bytes"):
        assets.npm(archive, package, tmp_path)


@pytest.mark.parametrize(
    "directory", ["worker/Lib/site-packages", "worker/Scripts", "openclaw/node_modules"]
)
@pytest.mark.parametrize("mutation", ["change", "add", "remove"])
def test_full_runtime_inventory_detects_mutations_and_unexpected_files(
    tmp_path, directory, mutation
):
    for relative in ("worker/Lib/site-packages", "worker/Scripts", "openclaw/node_modules"):
        target = tmp_path / relative
        target.mkdir(parents=True)
        (target / "runtime").write_bytes(b"original")
    saved = {
        "files": assets.inventory(tmp_path),
        "archives": {},
        "archive_verified_files": 3,
        "generated_files": [],
    }
    path = tmp_path / "runtime-assets.json"
    path.write_bytes(e.encode(saved))
    expected = assets._summary(saved, e.file_hash(path))
    assert assets.verify(tmp_path, expected) == expected
    target = tmp_path / directory / "runtime"
    if mutation == "change":
        target.write_bytes(b"changed")
    elif mutation == "add":
        target.with_name("unexpected.pyc").write_bytes(b"added")
    else:
        target.unlink()
    with pytest.raises(e.Refused, match="runtime_assets_changed"):
        assets.verify(tmp_path, expected)


def test_wheel_data_script_relocation_is_byte_verified(tmp_path):
    target = tmp_path / "worker/Scripts/postinstall.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"print('installed')")
    archive = tmp_path / "package.whl"
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr("package-1.data/scripts/postinstall.py", target.read_bytes())
    assert assets.wheel(archive, tmp_path / "worker/Lib/site-packages", tmp_path) == {
        "worker/Scripts/postinstall.py"
    }
    target.write_bytes(b"changed")
    with pytest.raises(e.Refused, match="locked_archive_bytes"):
        assets.wheel(archive, tmp_path / "worker/Lib/site-packages", tmp_path)


@pytest.mark.parametrize("declared", [False, True])
def test_npm_allows_only_declared_bin_shebang_normalization(tmp_path, declared):
    import json

    package = tmp_path / "openclaw/node_modules/dependency"
    package.mkdir(parents=True)
    original = b"#!/usr/bin/env node\r\nconsole.log('ok');\r\n"
    metadata = json.dumps({"bin": {"tool": "bin.js"}} if declared else {}).encode()
    (package / "package.json").write_bytes(metadata)
    (package / "bin.js").write_bytes(original.replace(b"node\r\n", b"node\n"))
    archive = tmp_path / "dependency.tgz"
    with tarfile.open(archive, "w:gz") as source:
        for name, data in (("package.json", metadata), ("bin.js", original)):
            member = tarfile.TarInfo("package/" + name)
            member.size = len(data)
            source.addfile(member, io.BytesIO(data))
    if declared:
        assets.npm(archive, package, tmp_path)
        (package / "bin.js").write_bytes(original.replace(b"\r\n", b"\n"))
    with pytest.raises(e.Refused, match="locked_archive_bytes"):
        assets.npm(archive, package, tmp_path)
