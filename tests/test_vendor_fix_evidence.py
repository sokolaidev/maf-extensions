"""Vendor declarations supplement scanner metadata only for verified APK identities."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import plan_platform_remediation as planner  # noqa: E402
import platform_remediation as remediation  # noqa: E402
import vendor_fix_evidence as vendor  # noqa: E402
from container_release import read, write  # noqa: E402
from test_go_remediation import retained  # noqa: E402
from test_platform_remediation import apk  # noqa: E402
from test_platform_remediation import checkout as checkout

pytestmark = pytest.mark.workflow


def feeds(monkeypatch, directory):
    def download(url):
        name = next(name for name, (_, endpoint) in vendor.SOURCES.items() if url == endpoint)
        return json.dumps(
            {
                "reponame": "wolfi" if name == "wolfi" else name.removeprefix("alpine-"),
                "distroversion": "v3.24",
                "archs": ["x86_64", "aarch64"],
                "packages": [
                    {
                        "pkg": {
                            "name": "glibc",
                            "secfixes": {"2.43-r2": ["CVE-apk"], "0": ["CVE-unaffected"]},
                        }
                    }
                ],
            }
        ).encode()

    monkeypatch.setattr(vendor, "download", download)
    return vendor.acquire(directory)


def stale_match(installed="2.43-r0"):
    match = apk(installed=installed)
    match["artifact"]["metadata"] = {"originPackage": "glibc", "architecture": "x86_64"}
    match["vulnerability"].update(namespace="nvd:cpe", fix={"state": "unknown", "versions": []})
    return match


@pytest.mark.parametrize("guest_path", ["/lib/apk/db/installed", "/usr/lib/apk/db/installed"])
def test_vendor_fix_enables_rebuild_without_rewriting_scanner_evidence(
    checkout, tmp_path, monkeypatch, guest_path
):
    work = tmp_path / "work"
    snapshot = feeds(monkeypatch, work / "vendor-security")
    match = stale_match()
    match["artifact"]["locations"] = [{"path": guest_path}]
    directory = retained(work / "evidence", [match], profile="graphviz")
    original = (directory / "grype.json").read_bytes()
    result = planner.plan(checkout, [directory], vendor=snapshot)
    row = result["findings"][0]
    assert row["status"] == "rebuild-required"
    assert row["target"] == "2.43-r2"
    assert row["fix"] == {"state": "unknown", "versions": []}
    assert result["vendorEvidence"] == read(work / "vendor-security/receipt.json")
    assert not planner.proposal(checkout, result)
    assert (directory / "grype.json").read_bytes() == original
    assert vendor.fixes(snapshot, match["artifact"], "CVE-unaffected") == []
    candidate = stale_match("2.43-r2")
    candidate["artifact"]["locations"] = [{"path": guest_path}]
    sbom = {"artifacts": [candidate["artifact"]]}
    remediation.verify_packages(result, sbom, "graphviz")
    assert remediation.vendor_resolutions([candidate], [row], sbom)[0] == []


@pytest.mark.parametrize("mutation", ["bytes", "url", "age", "future", "missing"])
def test_retained_vendor_evidence_refuses_tampering_and_expiry(tmp_path, monkeypatch, mutation):
    directory = tmp_path / "vendor"
    monkeypatch.setattr(vendor, "now", lambda: "2026-11-11T12:00:00Z")
    feeds(monkeypatch, directory)
    receipt = read(directory / "receipt.json")
    if mutation == "bytes":
        (directory / "wolfi.json").write_bytes(b"{}")
    elif mutation == "url":
        receipt["sources"]["wolfi"]["url"] = "https://example.org/security.json"
    elif mutation in {"age", "future"}:
        receipt["fetchedAt"] = (
            "2026-11-09T12:00:00Z" if mutation == "age" else "2026-11-11T12:00:01Z"
        )
    else:
        del receipt["sources"]["wolfi"]
    write(directory / "receipt.json", receipt)
    with pytest.raises(ValueError):
        vendor.load(directory)


def test_download_refuses_unapproved_origins_and_redirects():
    with pytest.raises(ValueError, match="Unapproved"):
        vendor.download("https://example.org/security.json")
    with pytest.raises(ValueError, match="redirects"):
        vendor.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.org")


@pytest.mark.parametrize(
    "mutation", ["origin", "architecture", "vendor", "family", "zero", "location"]
)
def test_vendor_planner_requires_exact_supported_identity(
    checkout, tmp_path, monkeypatch, mutation
):
    snapshot = feeds(monkeypatch, tmp_path / "vendor")
    match = stale_match()
    if mutation in {"origin", "architecture"}:
        match["artifact"]["metadata"][
            "originPackage" if mutation == "origin" else "architecture"
        ] = "other"
    elif mutation == "vendor":
        match["artifact"]["purl"] = "pkg:apk/alpine/glibc@2.43-r0"
    elif mutation == "location":
        match["artifact"]["locations"] = [{"path": "/other/lib/apk/db/installed"}]
    else:
        snapshot["packages"]["wolfi"]["glibc"] = {
            "2.44-r2" if mutation == "family" else "0": ["CVE-apk"]
        }
    result = planner.plan(
        checkout, [retained(tmp_path, [match], profile="graphviz")], vendor=snapshot
    )
    assert result["affectedProfiles"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "old",
        "family",
        "origin",
        "architecture",
        "vendor",
        "location",
        "id",
        "inventory",
        "namespace",
        "scanner-fix",
        "duplicate",
    ],
)
def test_stale_nvd_resolution_requires_fixed_inventory(tmp_path, monkeypatch, mutation):
    snapshot = feeds(monkeypatch, tmp_path / "vendor")
    original = stale_match()
    proof = vendor.fixes(snapshot, original["artifact"], "CVE-apk")[0]
    rows = [
        {"id": "CVE-apk", "component": "glibc", "status": "rebuild-required", "vendorFix": proof}
    ]
    match = stale_match("2.43-r2")
    actual = copy.deepcopy(match["artifact"])
    if mutation in {"old", "family"}:
        match = stale_match("2.43-r1" if mutation == "old" else "2.44-r2")
        actual = copy.deepcopy(match["artifact"])
    elif mutation in {"origin", "architecture"}:
        actual["metadata"]["originPackage" if mutation == "origin" else "architecture"] = "other"
    elif mutation == "vendor":
        actual["purl"] = "pkg:apk/alpine/glibc@2.43-r2"
    elif mutation == "location":
        actual["locations"].append({"path": "/other/lib/apk/db/installed"})
    elif mutation == "id":
        match["vulnerability"]["id"] = "CVE-other"
    elif mutation == "inventory":
        actual["version"] = "2.43-r0"
    elif mutation == "namespace":
        match["vulnerability"]["namespace"] = "wolfi:distro:wolfi:rolling"
    elif mutation == "scanner-fix":
        match["vulnerability"]["fix"] = {"state": "fixed", "versions": ["2.43-r3"]}
    inventory = {"artifacts": [actual, actual] if mutation == "duplicate" else [actual]}
    if mutation == "duplicate":
        with pytest.raises(ValueError, match="Ambiguous"):
            remediation.vendor_resolutions([match], rows, inventory)
        return
    before = copy.deepcopy(match)
    outstanding, resolved = remediation.vendor_resolutions([match], rows, inventory)
    assert match == before
    assert outstanding == ([] if mutation is None else [match])
    assert bool(resolved) == (mutation is None)
