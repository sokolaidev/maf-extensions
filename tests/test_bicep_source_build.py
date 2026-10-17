"""Source builds preserve the selected route, dependency fixes and candidate identity."""

from __future__ import annotations

import copy
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bicep_source as source  # noqa: E402
import build_bicep_image as builder  # noqa: E402
import prepare_container_release_dispatch as dispatch  # noqa: E402
from container_release import (  # noqa: E402
    digest,
    empty_catalogue,
    read,
    reserve,
    validate_identity,
    validate_transition,
    write,
)
from container_release_approval import evidence_rows  # noqa: E402

pytestmark = pytest.mark.workflow
CONTEXT = ROOT / "images/bicep-sandbox"
POLICY = source.policy(CONTEXT / "source.json")


def receipt():
    return {
        "route": "source",
        **copy.deepcopy(POLICY),
        "policySha256": source.sha256(CONTEXT / "source.json"),
        "builderSha256": source.sha256(Path(source.__file__)),
        "compilerSha256": "a" * 64,
    }


def candidate(route="source"):
    return {
        "profile": "bicep",
        "version": "0.1.0",
        "sourceCommit": "a" * 40,
        "sourceRef": "refs/heads/main",
        "registryDigest": "sha256:" + "b" * 64,
        "assessedManifestDigest": "sha256:" + "b" * 64,
        "imageId": "sha256:" + "c" * 64,
        "attemptId": "123",
        "bicepRoute": route,
    }


def test_upstream_route_uses_existing_recipe_without_source_inputs(monkeypatch):
    monkeypatch.setattr(builder, "policy", lambda *_: pytest.fail("upstream needs no SDK"))
    command = builder.command("upstream", "test:upstream")
    assert "--file" not in command and "--build-arg" not in command
    assert command[-1] == str(CONTEXT)


def test_source_route_pins_sdk_and_requires_matching_compiler_version(monkeypatch):
    command = builder.command("source", "test:source")
    assert command[command.index("--file") + 1] == str(CONTEXT / "source.Dockerfile")
    assert f"SDK_IMAGE={POLICY['sdkImage']}" in command
    monkeypatch.setattr(builder, "read_pin", lambda *_: ("v99.0.0", "a" * 64))
    with pytest.raises(ValueError, match="Review"):
        builder.command("source", "test:source")


@pytest.mark.parametrize("route,profile", [("other", "bicep"), ("source", "graphviz")])
def test_invalid_route_is_rejected(route, profile):
    with pytest.raises(ValueError):
        builder.validate_route(route, profile)


@pytest.mark.parametrize("existing_pkcs", [False, True])
def test_patch_changes_existing_pin_and_adds_transitive_pin(tmp_path, existing_pkcs):
    path = tmp_path / "Directory.Packages.props"
    pkcs = (
        '<PackageVersion Include="System.Security.Cryptography.Pkcs" Version="10.0.10" />'
        if existing_pkcs
        else ""
    )
    path.write_text(
        "<Project><PropertyGroup><CentralPackageTransitivePinningEnabled>true</CentralPackageTransitivePinningEnabled></PropertyGroup>"
        '<ItemGroup><PackageVersion Include="System.Security.Cryptography.Xml" Version="10.0.10" />'
        '<PackageVersion Include="MessagePack" Version="3.1.7" />'
        '<PackageVersion Include="Unrelated" Version="1.0.0" />' + pkcs + "</ItemGroup></Project>"
    )
    source.patch_packages(path, POLICY["packages"])
    pins = {
        item.attrib["Include"]: item.attrib["Version"]
        for item in ET.parse(path).findall("./ItemGroup/PackageVersion")
    }
    assert pins == {**POLICY["packages"], "Unrelated": "1.0.0"}


@pytest.mark.parametrize("name", sorted(POLICY["packages"]))
@pytest.mark.parametrize("mode", ["patched", "stale", "missing", "duplicate"])
def test_restore_graph_must_contain_only_patched_versions(tmp_path, name, mode):
    libraries = {f"{package}/{version}": {} for package, version in POLICY["packages"].items()}
    if mode in {"stale", "missing"}:
        del libraries[f"{name}/{POLICY['packages'][name]}"]
    if mode in {"stale", "duplicate"}:
        libraries[f"{name}/0.0.1"] = {}
    path = tmp_path / "project.assets.json"
    path.write_text(json.dumps({"libraries": libraries}))
    if mode == "patched":
        assert source.resolved_packages(path, POLICY["packages"]) == POLICY["packages"]
    else:
        with pytest.raises(ValueError, match="dependency differs"):
            source.resolved_packages(path, POLICY["packages"])


def test_new_source_policy_requires_messagepack_override(tmp_path):
    data = copy.deepcopy(POLICY)
    del data["packages"]["MessagePack"]
    path = tmp_path / "source.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid pinned"):
        source.policy(path)


@pytest.mark.parametrize(
    "field",
    [
        "sourceCommit",
        "sdkImage",
        "packages",
        "policySha256",
        "builderSha256",
        "compilerSha256",
        "route",
    ],
)
def test_source_receipt_rejects_drift(field):
    data = receipt()
    data[field] = "changed"
    with pytest.raises(ValueError, match="receipt differs"):
        source.verify_receipt(data, "a" * 64, CONTEXT)


def test_source_receipt_is_accepted_and_approval_displays_it():
    data = receipt()
    assert source.verify_receipt(data, "a" * 64, CONTEXT) == data
    selected = candidate()
    runtime = {"profile": "bicep", "imageId": selected["imageId"], "bicep": {"build": data}}
    rows = evidence_rows({"runtime.json": runtime}, selected)
    assert ("Bicep acquisition route", "source") in rows
    assert ("Bicep patched packages", POLICY["packages"]) in rows


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("kind", ["bicep", "bicepAvm"])
@pytest.mark.parametrize("mode", ["complete", "missing", "stale", "os-only"])
def test_inventory_must_include_patched_application_packages(mode, kind, legacy):
    data = receipt()
    if legacy:
        del data["packages"]["MessagePack"]
    artifacts = [
        {"name": name, "version": version, "type": "dotnet"}
        for name, version in data["packages"].items()
    ]
    if mode == "missing":
        artifacts.pop(0)
    elif mode == "stale":
        artifacts[0]["version"] = "10.0.10"
    elif mode == "os-only":
        artifacts = [{"name": "icu", "version": "1", "type": "rpm"}]
    runtime = {kind: {"build": data}}
    if mode == "complete":
        source.verify_inventory({"artifacts": artifacts}, runtime)
    else:
        with pytest.raises(ValueError, match="inventory"):
            source.verify_inventory({"artifacts": artifacts}, runtime)


def test_reservation_preserves_route_and_refuses_switching_it():
    selected = candidate()
    catalogue = reserve(empty_catalogue(), selected, "2026-10-16T00:00:00Z")
    validate_transition(empty_catalogue(), catalogue)
    assert catalogue["releases"]["bicep/0.1.0"]["candidate"] == selected
    with pytest.raises(ValueError, match="cannot change"):
        reserve(catalogue, selected | {"bicepRoute": "upstream"}, "2026-10-16T00:00:01Z")
    with pytest.raises(ValueError, match="route"):
        validate_identity(selected | {"bicepRoute": "other"})


@pytest.mark.parametrize("selected_route", ["source", "upstream"])
def test_retry_recovers_original_source_candidate_without_building(
    monkeypatch, tmp_path, selected_route
):
    selected = candidate()
    monkeypatch.setattr(dispatch, "context", lambda: (selected["sourceCommit"], "123", "2"))
    monkeypatch.setattr(dispatch, "History", lambda: SimpleNamespace(head=lambda: None))
    monkeypatch.setattr(dispatch, "retained_available", lambda *_: True)
    monkeypatch.setattr(dispatch, "run", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "read", lambda *_: selected)
    monkeypatch.setattr(dispatch, "validate_preparation", lambda *_: None)
    monkeypatch.setattr(dispatch, "validate_layout", lambda *_: {"imageId": selected["imageId"]})
    monkeypatch.setattr(dispatch, "prepare", lambda *a, **kw: pytest.fail("retry must not compile"))
    monkeypatch.setattr(dispatch, "outputs", lambda *_: None)
    if selected_route == "source":
        dispatch.dispatch(
            "bicep", "0.1.0", tmp_path / "build", tmp_path / "selection", bicep_route=selected_route
        )
        assert read(tmp_path / "selection/candidate.json") == selected
    else:
        with pytest.raises(ValueError, match="differs"):
            dispatch.dispatch(
                "bicep",
                "0.1.0",
                tmp_path / "build",
                tmp_path / "selection",
                bicep_route=selected_route,
            )


def test_release_and_security_workflows_offer_both_routes():
    for name in ("container-image-release", "image-security"):
        workflow = yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())
        options = workflow[True]["workflow_dispatch"]["inputs"]["bicep_route"]
        if name == "image-security":
            assert options["options"] == ["policy", "upstream", "source"]
            assert options["default"] == "policy"
        else:
            assert options["options"] == ["upstream", "source"]
            assert options["default"] == "upstream"
    release = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    step = next(
        step for step in release["jobs"]["prepare"]["steps"] if step.get("id") == "candidate"
    )
    assert step["env"]["BICEP_ROUTE"] == "${{ inputs.bicep_route }}"
    assert '--bicep-route "$BICEP_ROUTE"' in step["run"]


@pytest.mark.parametrize("mutation", [None, "changed", "missing-route", "wrong-image"])
def test_monitor_binds_source_inventory_to_original_candidate(tmp_path, mutation):
    from container_release_monitor import application_inventory

    selected = candidate()
    runtime = {"profile": "bicep", "imageId": selected["imageId"], "bicep": {"build": receipt()}}
    if mutation == "missing-route":
        runtime["bicep"]["build"].pop("route")
    if mutation == "wrong-image":
        runtime["imageId"] = "sha256:" + "d" * 64
    write(tmp_path / "runtime.json", runtime)
    write(tmp_path / "preparation-index.json", {"runtime.json": digest(tmp_path / "runtime.json")})
    selected["preparationEvidenceSha256"] = digest(tmp_path / "preparation-index.json")
    if mutation == "changed":
        write(tmp_path / "runtime.json", {})
    if mutation is None:
        assert application_inventory(tmp_path, selected, selected["imageId"]) == runtime
    else:
        with pytest.raises(ValueError):
            application_inventory(tmp_path, selected, selected["imageId"])
