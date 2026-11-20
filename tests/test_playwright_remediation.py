"""Bundled-runtime fixes must prove ownership, bounded source edits and actual scan improvement."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import plan_platform_remediation as planner  # noqa: E402
import platform_remediation as remediation  # noqa: E402
import playwright_remediation as bundled  # noqa: E402
from container_release import read, scan_result, write  # noqa: E402
from test_go_remediation import AT, IMAGE, finding, retained  # noqa: E402
from test_image_remediation import BOT, SOURCE, URL, FakeGitHub  # noqa: E402
from test_image_remediation import app_key as app_key  # noqa: E402
from test_platform_remediation import copy_checkout  # noqa: E402

pytestmark = pytest.mark.workflow
PREFIX = "/usr/local/lib/python3.14/dist-packages"
NODE = f"{PREFIX}/playwright/driver/node"


def inventory(package="1.65.0", node="26.13.0"):
    digest = hashlib.sha256(node.encode()).digest()
    component = finding(NODE, "node", node, "26.13.1", "CVE-2026-123865")["artifact"]
    component["type"] = "binary"
    owner = {
        "id": "python-owner",
        "name": "playwright",
        "type": "python",
        "version": package,
        "purl": f"pkg:pypi/playwright@{package}",
        "locations": [{"path": f"{PREFIX}/playwright-{package}.dist-info/RECORD"}],
        "metadata": {
            "name": "playwright",
            "version": package,
            "sitePackagesRootPath": PREFIX,
            "files": [
                {
                    "path": "playwright/driver/node",
                    "size": "100",
                    "digest": {
                        "algorithm": "sha256",
                        "value": base64.urlsafe_b64encode(digest).decode().rstrip("="),
                    },
                }
            ],
        },
    }
    return {
        "source": {"type": "image", "metadata": {"imageID": IMAGE}},
        "artifacts": [component, owner],
        "files": [
            {
                "location": {"path": NODE},
                "metadata": {"size": 100},
                "digests": [{"algorithm": "sha256", "value": digest.hex()}],
            }
        ],
    }


def index():
    releases = {}
    for value in ("1.65.0", "1.65.2", "1.66.0", "2.0.0"):
        name = f"playwright-{value}-py3-none-manylinux1_x86_64.whl"
        releases[value] = [
            {
                "filename": name,
                "packagetype": "bdist_wheel",
                "yanked": False,
                "url": f"https://files.pythonhosted.org/packages/aa/{name}",
                "digests": {"sha256": hashlib.sha256(name.encode()).hexdigest()},
            }
        ]
    return {"info": {"name": "playwright"}, "releases": releases}


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    copy_checkout(ROOT, root)
    (root / bundled.MANIFEST).write_text("playwright==1.65.0\npypdf==7.0.0\n", newline="\n")
    (root / bundled.LOCK).write_text(
        "# pinned inputs\nplaywright==1.65.0 \\\n    --hash=sha256:"
        + "a" * 64
        + "\n    # via -r requirements.in\npypdf==7.0.0\n",
        newline="\n",
    )
    monkeypatch.setattr(bundled, "now", lambda: AT)
    documents = {
        bundled.INDEX: json.dumps(index()).encode(),
        f"{bundled.UPSTREAM}1.65.0/NODE_VERSION": b"26.13.0\n",
        f"{bundled.UPSTREAM}1.65.2/NODE_VERSION": b"26.13.1\n",
    }
    monkeypatch.setattr(bundled, "download", documents.__getitem__)
    return root


@pytest.fixture
def prepared(source, tmp_path, monkeypatch):
    work = tmp_path / "work"
    sbom = inventory()
    match = finding(NODE, "node", "26.13.0", "26.13.1", "CVE-2026-123865")
    match["artifact"] = sbom["artifacts"][0]
    path = retained(work / "evidence", [match], profile="document-export")
    write(path / "sbom.syft.json", sbom)
    metadata = bundled.acquire(source, work / "playwright-security")
    expected = planner.plan(source, [path], owner=metadata)
    write(work / "receipt.json", {"source": SOURCE, "directories": [path.name]})
    write(work / "plan.json", expected)
    monkeypatch.setattr(remediation, "git", lambda *_: SOURCE)
    return source, work, path, metadata


def results_for(source, work, path, output):
    expected = read(work / "plan.json")
    files = planner.proposal(source, expected)
    for variant in ("baseline", "candidate"):
        directory = output / f"{variant}-document-export"
        source_fixed = bundled.pin(source) == "1.65.2"
        fixed = variant == "candidate" or source_fixed
        sbom = inventory("1.65.2" if fixed else "1.65.0", "26.13.1" if fixed else "26.13.0")
        report = read(path / "grype.json")
        report["matches"] = [] if fixed else report["matches"]
        write(directory / "sbom.syft.json", sbom)
        write(directory / "grype.json", report)
        write(directory / "assessment.json", scan_result(report, IMAGE, AT, sbom=sbom))
        write(
            directory / "runtime.json",
            {
                "profile": "document-export",
                "imageId": IMAGE,
                "passed": True,
                "containersRemaining": [],
            },
        )
        write(
            directory / "build.json",
            {"source_commit": SOURCE, "profile": "document-export", "local_image_id": IMAGE},
        )
        write(directory / "vendor.json", [])
        write(
            directory / "qualification.json",
            {
                "schemaVersion": 1,
                "source": SOURCE,
                "profile": "document-export",
                "variant": variant,
                "route": "upstream",
                "plan": planner.digest(work / "plan.json"),
                "inputs": expected["inputs"]
                | (planner.file_hashes(files) if variant == "candidate" else {}),
            },
        )


def test_acquisition_maps_verified_ownership_and_updates_only_the_owner_pin_and_hashes(prepared):
    root, work, path, _ = prepared
    assert bundled.needed([path])
    expected, files = remediation.validate(root, work)
    assert expected["affectedProfiles"] == ["document-export"]
    assert expected["findings"][0]["target"] == "26.13.1"
    assert expected["findings"][0]["ownerVersion"] == "1.65.2"
    assert set(files) == {bundled.MANIFEST, bundled.LOCK}
    assert files[bundled.MANIFEST] == b"playwright==1.65.2\npypdf==7.0.0\n"
    assert files[bundled.LOCK].startswith(b"# pinned inputs\nplaywright==1.65.2 \\\n")
    assert files[bundled.LOCK].endswith(b"    # via -r requirements.in\npypdf==7.0.0\n")


def test_multiple_fixes_share_the_final_owner_even_when_source_already_fixes_one(prepared):
    root, _, path, metadata = prepared
    sbom = inventory(node="26.12.0")
    report = read(path / "grype.json")
    match = report["matches"][0]
    match["artifact"] = sbom["artifacts"][0]
    earlier = deepcopy(match)
    earlier["vulnerability"].update(
        id="CVE-earlier", fix={"state": "fixed", "versions": ["26.13.0"]}
    )
    report["matches"].append(earlier)
    write(path / "sbom.syft.json", sbom)
    write(path / "grype.json", report)
    observed = read(path / "observation.json")
    observed["assessment"] = scan_result(report, IMAGE, AT, sbom=sbom) | {
        "digest": observed["candidate"]["registryDigest"]
    }
    write(path / "observation.json", observed)
    result = planner.plan(root, [path], owner=metadata)
    assert {row["status"] for row in result["findings"]} == {"source-pin-sufficient", "update"}
    assert {row["ownerVersion"] for row in result["findings"]} == {"1.65.2"}
    assert len(result["updates"]) == 1
    remediation.verify_packages(result, inventory("1.65.2", "26.13.1"), "document-export")


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_proposal_preserves_comments_and_native_line_endings(source, tmp_path, newline):
    manifest = b"# tested playwright==1.65.0\nplaywright==1.65.0\npypdf==7.0.0\n"
    (source / bundled.MANIFEST).write_bytes(manifest.replace(b"\n", newline))
    lock = (source / bundled.LOCK).read_bytes().replace(b"\n", newline)
    (source / bundled.LOCK).write_bytes(lock)
    metadata = bundled.acquire(source, tmp_path / "metadata")
    sbom = inventory()
    _, update = bundled.plan(
        source, sbom, sbom["artifacts"][0], {"state": "fixed", "versions": ["26.13.1"]}, metadata
    )
    assert update is not None
    files = bundled.proposal(source, update)
    assert files[bundled.MANIFEST] == (
        b"# tested playwright==1.65.0\nplaywright==1.65.2\npypdf==7.0.0\n".replace(b"\n", newline)
    )
    assert files[bundled.LOCK].endswith(
        b"    # via -r requirements.in\npypdf==7.0.0\n".replace(b"\n", newline)
    )


@pytest.mark.parametrize("mutation", ["duplicate", "range", "lock-mismatch"])
def test_ambiguous_or_divergent_owner_pins_are_refused(source, mutation):
    if mutation == "lock-mismatch":
        path = source / bundled.LOCK
        path.write_bytes(path.read_bytes().replace(b"1.65.0", b"1.65.1"))
    else:
        path = source / bundled.MANIFEST
        path.write_bytes(
            path.read_bytes()
            + (b"playwright==1.65.0\n" if mutation == "duplicate" else b"playwright>=1.65.0\n")
        )
    with pytest.raises(ValueError):
        bundled.pin(source)


def test_acquire_plan_retains_owner_metadata_only_for_applicable_owned_node(prepared, monkeypatch):
    root, work, path, metadata = prepared
    monkeypatch.setattr(planner.vendor_fix_evidence, "acquire", lambda _: {"receipt": {}})
    calls = []
    monkeypatch.setattr(bundled, "acquire", lambda r, p: calls.append((r, p)) or metadata)
    assert planner.acquire_plan(root, [path])["updates"]
    assert calls == [(root, work / "playwright-security")]
    sbom = read(path / "sbom.syft.json")
    sbom["files"] = []
    write(path / "sbom.syft.json", sbom)
    calls.clear()
    assert planner.acquire_plan(root, [path])["updates"] == []
    assert calls == []


@pytest.mark.parametrize(
    "mutation", ["record", "hash", "size", "path", "duplicate", "purl", "root"]
)
def test_path_or_package_name_alone_cannot_claim_the_node_binary(mutation):
    sbom = inventory()
    component, package = sbom["artifacts"]
    if mutation == "record":
        package["metadata"]["files"] = []
    elif mutation == "hash":
        sbom["files"][0]["digests"][0]["value"] = "f" * 64
    elif mutation == "size":
        sbom["files"][0]["metadata"]["size"] = 200
    elif mutation == "path":
        component["locations"][0]["path"] = "/usr/bin/node"
    elif mutation == "duplicate":
        sbom["artifacts"].append(deepcopy(package))
    elif mutation == "purl":
        package["purl"] = "pkg:pypi/another@1.65.0"
    else:
        package["metadata"]["sitePackagesRootPath"] = (
            "/opt/../usr/local/lib/python3.14/dist-packages"
        )
    assert bundled.owner(sbom, component) is None


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "other-major", "prerelease", "already-fixed", "unfixed-owner", "minor-only"],
)
def test_unavailable_or_incompatible_fixes_do_not_propose_routine_upgrades(prepared, mutation):
    root, _, path, metadata = prepared
    report = read(path / "grype.json")
    fix = report["matches"][0]["vulnerability"]["fix"]
    if mutation == "unfixed-owner":
        metadata["candidateNode"] = "26.13.0"
    elif mutation == "minor-only":
        data = index()
        del data["releases"]["1.65.2"]
        assert bundled.select(data, "1.65.0") == "1.65.0"
        metadata.update(after="1.65.0", candidateNode="26.13.0")
    else:
        fix["versions"] = {
            "unknown": [],
            "other-major": ["27.0.0"],
            "prerelease": ["26.13.1-rc1"],
            "already-fixed": ["26.13.0"],
        }[mutation]
    write(path / "grype.json", report)
    observed = read(path / "observation.json")
    observed["assessment"] = scan_result(report, IMAGE, AT, sbom=read(path / "sbom.syft.json")) | {
        "digest": observed["candidate"]["registryDigest"]
    }
    write(path / "observation.json", observed)
    result = planner.plan(root, [path], owner=metadata)
    assert not result["updates"] and not result["affectedProfiles"]


@pytest.mark.parametrize("mutation", ["hash", "url", "source", "age", "missing", "selection"])
def test_retained_vendor_evidence_is_rechecked_before_edits(prepared, mutation):
    root, work, _, _ = prepared
    directory = work / "playwright-security"
    receipt = read(directory / "receipt.json")
    if mutation == "hash":
        (directory / "candidate-node.txt").write_text("26.99.99\n")
    elif mutation == "url":
        receipt["sources"]["candidate-node.txt"]["url"] = "https://example.invalid/node"
    elif mutation == "source":
        receipt["before"] = "1.64.0"
    elif mutation == "age":
        receipt["fetchedAt"] = "2020-01-01T00:00:00Z"
    elif mutation == "missing":
        del receipt["sources"]["source-node.txt"]
    else:
        receipt["sources"]["candidate-node.txt"]["url"] = f"{bundled.UPSTREAM}1.66.0/NODE_VERSION"
    write(directory / "receipt.json", receipt)
    with pytest.raises(ValueError):
        remediation.validate(root, work)


@pytest.mark.parametrize(
    "mutation", ["yanked", "origin", "hash", "duplicate", "no-linux", "wrong-package"]
)
def test_pypi_candidates_require_reviewed_origin_and_usable_hashed_wheels(mutation):
    data = index()
    item = data["releases"]["1.65.2"][0]
    if mutation == "yanked":
        item["yanked"] = True
    elif mutation == "origin":
        item["url"] = "http://files.pythonhosted.org/" + item["filename"]
    elif mutation == "hash":
        item["digests"]["sha256"] = "invalid"
    elif mutation == "duplicate":
        data["releases"]["1.65.2"].append(deepcopy(item))
    else:
        item["filename"] = (
            item["filename"].replace("manylinux1_x86_64", "win_amd64")
            if mutation == "no-linux"
            else "another.whl"
        )
        item["url"] = "https://files.pythonhosted.org/packages/aa/" + item["filename"]
    assert bundled.select(data, "1.65.0") == "1.65.0"


def test_complete_platform_qualification_delivers_owned_owner_update(
    prepared, tmp_path, monkeypatch
):
    root, work, path, _ = prepared
    results = tmp_path / "results"
    results_for(root, work, path, results)
    report = remediation.qualify(root, work, results)
    assert report["status"] == "ready" and report["unresolved"] == []
    client: Any = FakeGitHub()
    client.branch = remediation.PlatformGitHub.branch
    monkeypatch.setattr(remediation, "current", lambda *_: None)
    delivered = remediation.publish(
        client,
        read(work / "receipt.json"),
        report,
        remediation.validate(root, work)[1],
        BOT,
        URL,
        freshness=lambda *_: None,
    )
    assert delivered["status"] == "created"
    assert "bundled-runtime owner" in client.pulls[0]["body"]


def test_unchanged_unrelated_high_findings_remain_visible_without_blocking_owner_fix(
    prepared, tmp_path
):
    root, work, path, _ = prepared
    results = tmp_path / "results"
    results_for(root, work, path, results)
    for variant in ("baseline", "candidate"):
        directory = results / f"{variant}-document-export"
        sbom, report = read(directory / "sbom.syft.json"), read(directory / "grype.json")
        match = finding("/usr/bin/unrelated", "another", "1.0.0", "1.0.1", "CVE-unrelated")
        match["artifact"]["id"] = "unrelated-artifact"
        report["matches"].append(match)
        sbom["artifacts"].append(match["artifact"])
        write(directory / "sbom.syft.json", sbom)
        write(directory / "grype.json", report)
        write(directory / "assessment.json", scan_result(report, IMAGE, AT, sbom=sbom))
    report = remediation.qualify(root, work, results)
    assert report["status"] == "ready"
    assert [f["id"] for f in report["scans"][-1]["assessment"]["findings"]] == ["CVE-unrelated"]


@pytest.mark.parametrize(
    "mutation",
    [
        "old-node",
        "wrong-owner",
        "missing-node",
        "bad-ownership",
        "new-high",
        "database",
        "runtime",
        "inputs",
        "target-remains",
    ],
)
def test_candidate_must_prove_fixed_inventory_and_no_new_high_or_critical(
    prepared, tmp_path, mutation
):
    root, work, path, _ = prepared
    results = tmp_path / "results"
    results_for(root, work, path, results)
    directory = results / "candidate-document-export"
    sbom, report = read(directory / "sbom.syft.json"), read(directory / "grype.json")
    if mutation == "old-node":
        sbom = inventory("1.65.2", "26.13.0")
    elif mutation == "wrong-owner":
        sbom = inventory("1.65.0", "26.13.1")
    elif mutation == "missing-node":
        sbom["artifacts"] = sbom["artifacts"][1:]
    elif mutation == "bad-ownership":
        sbom["files"] = []
    elif mutation == "database":
        report["descriptor"]["db"]["status"]["built"] = "2026-11-06T11:00:00Z"
    elif mutation == "runtime":
        runtime = read(directory / "runtime.json")
        runtime["passed"] = False
        write(directory / "runtime.json", runtime)
    elif mutation == "inputs":
        identity = read(directory / "qualification.json")
        identity["inputs"][bundled.LOCK] = "f" * 64
        write(directory / "qualification.json", identity)
    else:
        match = finding(
            NODE,
            "node",
            "26.13.1",
            "26.13.2",
            "CVE-new" if mutation == "new-high" else "CVE-2026-123865",
        )
        match["artifact"] = sbom["artifacts"][0]
        report["matches"] = [match]
    write(directory / "sbom.syft.json", sbom)
    write(directory / "grype.json", report)
    write(directory / "assessment.json", scan_result(report, IMAGE, AT, sbom=sbom))
    if mutation in {"new-high", "target-remains"}:
        assert remediation.qualify(root, work, results)["status"] == "unresolved"
    else:
        with pytest.raises(ValueError):
            remediation.qualify(root, work, results)


def test_source_pin_already_contains_the_fix_requires_release_not_an_empty_pr(prepared, tmp_path):
    root, work, path, _ = prepared
    for name, raw in remediation.validate(root, work)[1].items():
        (root / name).write_bytes(raw)
    metadata = bundled.acquire(root, tmp_path / "fresh-metadata")
    expected = planner.plan(root, [path], owner=metadata)
    assert expected["findings"][0]["status"] == "source-pin-sufficient"
    assert planner.proposal(root, expected) == {}
    write(work / "plan.json", expected)
    for name in ("receipt.json", "index.json", "source-node.txt", "candidate-node.txt"):
        (work / "playwright-security" / name).write_bytes(
            (tmp_path / "fresh-metadata" / name).read_bytes()
        )
    results = tmp_path / "results"
    results_for(root, work, path, results)
    assert remediation.qualify(root, work, results)["status"] == "release-needed"


def test_acquisition_conflicts_include_the_build_consumed_hash_lock(prepared, monkeypatch):
    root, work, _, _ = prepared
    paths = []
    monkeypatch.setattr(remediation, "ROOT", root)
    monkeypatch.setattr(remediation, "acquire", lambda *a, **k: read(work / "plan.json"))
    monkeypatch.setattr(remediation, "competing_pulls", lambda _c, p: paths.append(p) or [123])
    outputs = []
    monkeypatch.setattr(remediation, "outputs", outputs.append)
    monkeypatch.setattr(sys, "argv", ["platform_remediation.py", "acquire", "--work", str(work)])
    remediation.main()
    assert paths == [{bundled.MANIFEST, bundled.LOCK}]
    assert outputs[0]["profiles"] == "[]"
