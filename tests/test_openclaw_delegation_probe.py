"""Installed importability must not bypass the credential authority admission gate."""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openclaw_delegation_probe")


@pytest.fixture
def metadata(module):
    return {
        "name": "openclaw",
        "version": module.VERSION,
        "exports": {module.SUBPATH: {"default": "./private.js"}},
    }


@pytest.mark.parametrize(
    "documentation,expected",
    [
        (
            "| `plugin-sdk/secret-egress-runtime` | not a third-party plugin API |",
            "private_official_plugin_api",
        ),
        ("| `plugin-sdk/secret-egress-runtime` | now public |", "support_not_established"),
        ("", "support_not_established"),
    ],
)
def test_export_or_changed_documentation_never_authorizes_owner(
    module, metadata, documentation, expected
):
    requests = []
    with pytest.raises(module.ProbeRefused, match=expected):
        module.admit(metadata, documentation, lambda: requests.append("credential"))
    assert requests == []


def test_missing_export_and_changed_version_refuse(module, metadata):
    metadata["exports"] = {}
    assert module.admission_reason(metadata, "") == "missing_export"
    metadata["version"] = "2099.1.1"
    assert module.admission_reason(metadata, "") == "unqualified_package"


@pytest.mark.parametrize(
    "version,expected",
    [
        ("v24.16.0", True),
        ("v24.15.0", False),
        ("v25.0.0", False),
        ("v26.0.0", False),
        ("v26.1.0", True),
        ("v27.0.0", True),
        ("v26.1.0-rc1", False),
        ("unknown", False),
    ],
)
def test_release_engine_boundary(module, version, expected):
    assert module.supported_node(version) is expected


def archive_fixture(tmp_path, module, monkeypatch, name="package/package.json"):
    archive = tmp_path / "package.tgz"
    data = b'{"name":"fixture"}'
    with tarfile.open(archive, "w:gz") as target:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        target.addfile(member, io.BytesIO(data))
    package = tmp_path / "installed"
    package.mkdir()
    (package / "package.json").write_bytes(data)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
    monkeypatch.setattr(module, "INTEGRITY", integrity)
    return archive, package


def test_installed_byte_mutation_is_not_registry_evidence(module, tmp_path, monkeypatch):
    archive, package = archive_fixture(tmp_path, module, monkeypatch)
    assert module.verify_files(archive, package)["files_verified"] == 1
    (package / "package.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(module.ProbeRefused, match="installed_file_mismatch"):
        module.verify_files(archive, package)


def test_archive_integrity_precedes_inspection(module, tmp_path, monkeypatch):
    archive, package = archive_fixture(tmp_path, module, monkeypatch)
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(module.ProbeRefused, match="archive_integrity_mismatch"):
        module.verify_files(archive, package)


def test_archive_paths_cannot_escape_package(module, tmp_path, monkeypatch):
    archive, package = archive_fixture(tmp_path, module, monkeypatch, "package/../outside.json")
    with pytest.raises(module.ProbeRefused, match="invalid_archive_path"):
        module.verify_files(archive, package)


@pytest.fixture
def installed_fixture(tmp_path):
    package = tmp_path / "node_modules" / "openclaw"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "openclaw",
                "type": "module",
                "exports": {
                    "./plugin-sdk/plugin-entry": "./public.js",
                    "./plugin-sdk/secret-egress-runtime": "./private.js",
                },
            }
        ),
        encoding="utf-8",
    )
    (package / "public.js").write_text("export function definePluginEntry() {}", encoding="utf-8")
    (package / "private.js").write_text(
        "if (process.env.PROBE_REAL_SECRET || process.env.NODE_OPTIONS) throw Error('leaked environment');"
        "export function withConfiguredModelEgress() { throw Error('must not acquire'); }",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    return tmp_path, package, home


def node_program():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the subprocess import controls")
    return node


def test_external_import_control_is_inert_and_has_no_ambient_secrets(
    module, installed_fixture, monkeypatch
):
    project, _, home = installed_fixture
    monkeypatch.setenv("PROBE_REAL_SECRET", "fixture-ambient-secret")
    monkeypatch.setenv("NODE_OPTIONS", "--this-should-not-reach-the-child")
    result = module.observe_imports(project, node_program(), home)
    assert [item["status"] for item in result["imports"]] == ["imported", "imported"]
    assert result["side_effect_attempts"] == []
    assert result["isolated_home_unchanged"]
    assert result["imports"][1]["exports"] == ["withConfiguredModelEgress"]


@pytest.mark.parametrize("schedule", ["immediate", "timer", "before_exit"])
@pytest.mark.parametrize(
    "imports,code,operation",
    [
        ("import net from 'node:net';", "net.connect(9, '127.0.0.1');", "network.connect"),
        (
            "import fs from 'node:fs';",
            "fs.writeFileSync('unwanted', 'data');",
            "files.writeFileSync",
        ),
        ("import {spawn} from 'node:child_process';", "spawn('never-started');", "process.spawn"),
    ],
)
def test_import_side_effects_are_visible_and_blocked(
    module, installed_fixture, imports, code, operation, schedule
):
    project, package, home = installed_fixture
    if schedule != "immediate":
        code = "setTimeout(() => { try { " + code + " } catch {} }, 25);"
    if schedule == "before_exit":
        code = "process.once('beforeExit', () => { " + code + " });"
    (package / "private.js").write_text(imports + code, encoding="utf-8")
    result = module.observe_imports(project, node_program(), home)
    assert result["imports"][0]["status"] == "imported"
    assert result["imports"][1]["status"] == ("failed" if schedule == "immediate" else "imported")
    assert result["side_effect_attempts"] == [operation]
    assert not (project / "unwanted").exists()


def test_import_that_never_settles_is_refused(module, installed_fixture, monkeypatch):
    project, package, home = installed_fixture
    (package / "private.js").write_text("setInterval(() => {}, 10);", encoding="utf-8")
    run = module.subprocess.run

    def bounded_run(*args, **kwargs):
        kwargs["timeout"] = 0.25
        return run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", bounded_run)
    with pytest.raises(module.ProbeRefused, match="import_timeout"):
        module.observe_imports(project, node_program(), home)


def test_checkout_refusal_precedes_artifact_access(module, tmp_path):
    (tmp_path / ".git").write_text("gitdir: fixture", encoding="utf-8")
    with pytest.raises(module.ProbeRefused, match="installation_must_be_outside_checkout"):
        module.probe(tmp_path, tmp_path / "absent.tgz", "never-started-node")
