"""Prepared Bicep artifacts must match the reviewed policy and OCI content pins."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bicep_dependencies as deps  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "images/bicep-sandbox"


def restored_module(tmp_path):
    directory = tmp_path / "br/mcr.microsoft.com/bicep$avm$res$network$virtual-network/0.7.2$"
    directory.mkdir(parents=True)
    data = b'{"parameters":{"name":{"type":"string"}}}'
    (directory / "main.json").write_bytes(data)
    manifest = {
        "layers": [
            {
                "mediaType": "application/vnd.ms.bicep.module.layer.v1+json",
                "size": len(data),
                "digest": "sha256:" + deps.sha256(data),
            }
        ],
    }
    deps.write_json(directory / "manifest", manifest)
    entry = {
        "reference": "br/public:avm/res/network/virtual-network:0.7.2",
        "manifest_sha256": deps.sha256((directory / "manifest").read_bytes()),
    }
    return directory, manifest, entry


def test_restored_artifacts_match_pinned_manifest(tmp_path):
    _, _, entry = restored_module(tmp_path)
    assert deps.verify_module(tmp_path, entry) == entry


@pytest.mark.parametrize("changed", ["manifest", "main.json"])
def test_changed_registry_content_is_refused(tmp_path, changed):
    directory, _, entry = restored_module(tmp_path)
    (directory / changed).write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed|does not match"):
        deps.verify_module(tmp_path, entry)


@pytest.mark.parametrize("case", ["missing", "duplicate", "unknown", "size"])
def test_malformed_layers_are_refused(tmp_path, case):
    directory, manifest, entry = restored_module(tmp_path)
    if case == "missing":
        manifest["layers"] = []
    elif case == "duplicate":
        manifest["layers"] *= 2
    elif case == "unknown":
        manifest["layers"][0]["mediaType"] = "application/unknown"
    else:
        manifest["layers"][0]["size"] = 0
    deps.write_json(directory / "manifest", manifest)
    entry["manifest_sha256"] = deps.sha256((directory / "manifest").read_bytes())
    with pytest.raises(ValueError):
        deps.verify_module(tmp_path, entry)


@pytest.mark.parametrize(
    "reference",
    [
        "br/public:avm/res/network/virtual-network:latest",
        "br:example.com/modules/network:0.7.2",
        "br/public:avm/res/../network:0.7.2",
        "br/public:avm/res/network/virtual-network:0.7.2'\nmodule evil",
    ],
)
def test_non_pinned_or_unsafe_references_are_refused(reference):
    with pytest.raises(ValueError, match="pinned"):
        deps.module_parts(reference)


def test_committed_manifest_matches_policy():
    path = CONTEXT / "dependencies.bicep-avm.policy.json"
    policy = deps.load_policy(path)
    manifest = json.loads((CONTEXT / "dependencies.bicep-avm.json").read_bytes())
    assert manifest["policy_sha256"] == deps.sha256(path.read_bytes())
    assert manifest["bicep_version"] == policy["bicep_version"]
    assert [entry["reference"] for entry in manifest["modules"]] == policy["modules"]
    assert manifest["excluded"] == policy["excluded"]
    assert f"ARG BICEP_VERSION=v{policy['bicep_version']}" in (CONTEXT / "Dockerfile").read_text()


@pytest.mark.parametrize("field", ["modules", "excluded", "bicep_version", "policy_sha256"])
def test_stale_lock_is_refused_before_restore(tmp_path, monkeypatch, field):
    manifest = json.loads((CONTEXT / "dependencies.bicep-avm.json").read_bytes())
    manifest[field] = [] if field in ("modules", "excluded") else "stale"
    path = tmp_path / "manifest.json"
    deps.write_json(path, manifest)
    monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="does not match"):
        deps.prepare(CONTEXT / "dependencies.bicep-avm.policy.json", path, tmp_path / "prepared")
    assert not (tmp_path / "prepared/receipt.json").exists()


def test_exclusions_require_reasons(tmp_path):
    policy = copy.deepcopy(deps.load_policy(CONTEXT / "dependencies.bicep-avm.policy.json"))
    policy["excluded"][0]["reason"] = " "
    path = tmp_path / "policy.json"
    deps.write_json(path, policy)
    with pytest.raises(ValueError, match="reason"):
        deps.load_policy(path)


def test_preparation_cannot_reuse_stale_output(tmp_path):
    output = tmp_path / "prepared"
    output.mkdir()
    with pytest.raises(ValueError, match="must not exist"):
        deps.prepare(Path("unused"), Path("unused"), output)


def test_generated_json_has_platform_independent_fingerprints(tmp_path):
    path = tmp_path / "manifest.json"
    deps.write_json(path, {"schema": 1})
    assert path.read_bytes() == b'{\n  "schema": 1\n}\n'


@pytest.mark.parametrize("rules,code", [(["BCP190"], 1), ([], 1), (["BCP035"], 2)])
def test_offline_probe_refuses_missing_modules_and_unexplained_failures(
    tmp_path, monkeypatch, rules, code
):
    deps.write_json(
        tmp_path / "receipt.json",
        {"modules": [{"reference": "br/public:avm/res/network/virtual-network:0.7.2"}]},
    )
    report = {"runs": [{"results": [{"ruleId": rule} for rule in rules]}]}
    monkeypatch.setattr(
        deps.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, code, stderr=json.dumps(report)),
    )
    with pytest.raises(ValueError, match="offline module probe failed"):
        deps.verify_offline(tmp_path)
