"""Source-built engine identity must survive installation without an archive fallback."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).parents[1] / "images/terraform-sandbox"
sys.path.insert(0, str(SOURCE))
import install  # noqa: E402


@pytest.fixture
def built(tmp_path):
    plan = install.load_plan("opentofu", "builtin")
    root = tmp_path / "built"
    root.mkdir()
    files = {
        "tofu": b"test binary",
        "LICENSE": b"license",
        "go.mod": b"module tofu",
        "go.sum": b"hashes",
    }
    for name, content in files.items():
        (root / name).write_bytes(content)
    record = {
        "source": plan["source_build"],
        "go_version": "go1.27.1",
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    (root / "build.json").write_text(json.dumps(record))
    return root, record


def test_source_build_cannot_fall_back_to_the_unpatched_release(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "download", lambda *args: pytest.fail("must not download"))
    plan = install.load_plan("opentofu", "builtin")
    with pytest.raises(ValueError, match="source build is required"):
        install.main("opentofu", "builtin", plan["version"], destination=tmp_path)


@pytest.mark.parametrize("tamper", ["source", "binary", "go.sum", "inventory"])
def test_changed_source_build_is_refused_before_execution(tmp_path, built, monkeypatch, tamper):
    root, record = built
    if tamper == "source":
        record["source"]["revision"] = "0" * 40
    elif tamper == "inventory":
        del record["files"]["go.mod"]
    else:
        (root / ("tofu" if tamper == "binary" else tamper)).write_bytes(b"changed")
    (root / "build.json").write_text(json.dumps(record))
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **kw: pytest.fail("must not execute"))
    plan = install.load_plan("opentofu", "builtin")
    with pytest.raises(ValueError):
        install.main(
            "opentofu", "builtin", plan["version"], str(root), destination=tmp_path / "image"
        )


def test_installed_binary_retains_source_and_module_provenance(tmp_path, built, monkeypatch):
    root, record = built
    plan = install.load_plan("opentofu", "builtin")
    monkeypatch.setattr(install, "download", lambda *args: pytest.fail("must not download"))
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            stdout=json.dumps({"terraform_version": plan["version"]}).encode()
        ),
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    output = tmp_path / "image"
    install.main(
        "opentofu",
        "builtin",
        plan["version"],
        str(root),
        destination=output,
        bin_directory=binaries,
    )
    installed = json.loads((output / "engine.json").read_text())
    assert installed["source_build"] == record
    assert installed["binary_sha256"] == record["files"]["tofu"]
    assert "archive_sha256" not in installed
    assert (output / "licenses/LICENSE").read_bytes() == b"license"
