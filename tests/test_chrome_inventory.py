"""Vendor Chrome must retain OS inventory and participate in upstream CVE matching."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chrome_inventory as chrome  # noqa: E402
import image_security_evidence as evidence  # noqa: E402
import prepare_container_release as preparation  # noqa: E402

pytestmark = pytest.mark.workflow
IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def inventory():
    return {
        "source": {"type": "image", "metadata": {"imageID": IMAGE}},
        "artifacts": [
            {
                "id": "vendor",
                "name": "google-chrome-stable",
                "version": "157.0.8103.38-1",
                "type": "deb",
                "metadata": {
                    "package": "google-chrome-stable",
                    "version": "157.0.8103.38-1",
                    "architecture": "amd64",
                    "files": [{"path": chrome.GUEST_BINARY}],
                },
            },
            {"id": "other", "name": "other-package", "type": "deb"},
        ],
        "files": [
            {
                "location": {"path": chrome.GUEST_BINARY, "layerID": "layer"},
                "metadata": {"type": "RegularFile"},
                "digests": [{"algorithm": "sha256", "value": "b" * 64}],
                "executable": {"format": "elf", "hasEntrypoint": True},
            }
        ],
    }


def test_vendor_metadata_alone_cannot_pass_inventory_verification(inventory):
    with pytest.raises(ValueError, match="upstream CVE inventory"):
        evidence.verify_inventory(inventory, IMAGE)


def test_browser_executable_without_vendor_metadata_is_refused(inventory):
    inventory["artifacts"].pop(0)
    with pytest.raises(ValueError, match="no vendor package"):
        chrome.component(inventory)


def test_document_export_cannot_claim_coverage_with_only_os_packages():
    with pytest.raises(ValueError, match="omits Google Chrome"):
        evidence.verify_profile_inventory({"artifacts": [{"name": "python"}]}, "document-export")


def test_enrichment_preserves_raw_packages_and_is_idempotent(tmp_path, inventory):
    path = tmp_path / "sbom.syft.json"
    original = json.dumps(inventory)
    path.write_text(original, encoding="utf-8")
    assert chrome.retain(tmp_path)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["artifacts"][:-1] == inventory["artifacts"]
    assert result["files"] == inventory["files"]
    assert result["source"] == inventory["source"]
    assert (tmp_path / "sbom.raw.syft.json").read_text(encoding="utf-8") == original
    browser = result["artifacts"][-1]
    assert browser["type"] == "binary"
    assert browser["purl"] == "pkg:generic/chrome@157.0.8103.38"
    assert browser["cpes"][0]["cpe"] == "cpe:2.3:a:google:chrome:157.0.8103.38:*:*:*:*:*:*:*"
    evidence.verify_inventory(result, IMAGE)
    assert not chrome.retain(tmp_path)
    assert (tmp_path / "sbom.raw.syft.json").read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "fault",
    ["duplicate", "version", "metadata", "architecture", "ownership", "file", "hash", "elf"],
)
def test_incomplete_browser_evidence_is_refused(inventory, fault):
    package = inventory["artifacts"][0]
    if fault == "duplicate":
        inventory["artifacts"].append(deepcopy(package))
    elif fault == "version":
        package["version"] = "unknown"
    elif fault == "metadata":
        package["metadata"]["version"] = "156.0.8078.79-1"
    elif fault == "architecture":
        package["metadata"]["architecture"] = "arm64"
    elif fault == "ownership":
        package["metadata"]["files"] = []
    elif fault == "file":
        inventory["files"] = []
    elif fault == "hash":
        inventory["files"][0]["digests"] = []
    else:
        inventory["files"][0]["executable"]["format"] = "pe"
    with pytest.raises(ValueError, match="Chrome"):
        chrome.component(inventory)


@pytest.mark.parametrize(
    "fault",
    ["location", "no-vendor-location", "location-shape", "version", "metadata", "files", "digest"],
)
def test_malformed_browser_inventory_has_a_validation_error(inventory, fault):
    if fault in {"location", "no-vendor-location"}:
        inventory["files"][0].pop("location")
        if fault == "no-vendor-location":
            inventory["artifacts"].pop(0)
    elif fault == "location-shape":
        inventory["files"][0]["location"] = None
    elif fault == "version":
        inventory["artifacts"][0]["version"] = None
    elif fault == "metadata":
        inventory["artifacts"][0]["metadata"] = None
    elif fault == "files":
        inventory["files"] = [None]
    else:
        inventory["files"][0]["digests"] = [{}]
    with pytest.raises(ValueError, match="Malformed Chrome"):
        chrome.component(inventory)


def test_stale_browser_mapping_is_not_reused(tmp_path, inventory):
    browser = chrome.component(inventory)
    inventory["artifacts"].append(browser)
    inventory["files"][0]["digests"][0]["value"] = "c" * 64
    (tmp_path / "sbom.syft.json").write_text(json.dumps(inventory), encoding="utf-8")
    with pytest.raises(ValueError, match="Conflicting Chrome"):
        chrome.retain(tmp_path)


def test_other_images_keep_their_original_inventory(tmp_path):
    original = '{"artifacts": [{"name": "python"}]}'
    path = tmp_path / "sbom.syft.json"
    path.write_text(original, encoding="utf-8")
    assert not chrome.retain(tmp_path)
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "sbom.raw.syft.json").exists()


def test_pr_inventory_is_enriched_before_validation(tmp_path, inventory, monkeypatch):
    (tmp_path / "sbom.syft.json").write_text(json.dumps(inventory), encoding="utf-8")
    (tmp_path / "build.json").write_text(
        json.dumps({"local_image_id": IMAGE, "profile": "document-export"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        sys, "argv", ["image_security_evidence.py", str(tmp_path), "--verify-inventory"]
    )
    evidence.main()
    chrome.verify(json.loads((tmp_path / "sbom.syft.json").read_text(encoding="utf-8")))


@pytest.mark.parametrize("require_clean", [True, False])
def test_release_and_monitor_scan_enriched_inventory(
    tmp_path, inventory, monkeypatch, require_clean
):
    commands = []

    def run(command, **kwargs):
        commands.append(command[:2])
        if command[:2] == ["syft", "scan"]:
            preparation.write(tmp_path / "sbom.syft.json", inventory)
        elif command[:2] == ["syft", "convert"]:
            chrome.verify(preparation.read(tmp_path / "sbom.syft.json"))

    def scan(command, **kwargs):
        chrome.verify(preparation.read(tmp_path / "sbom.syft.json"))
        assert command[1] == f"sbom:{tmp_path / 'sbom.syft.json'}"
        preparation.write(
            tmp_path / "grype.json",
            {
                "source": {"type": "image", "target": {"imageID": IMAGE}},
                "descriptor": {
                    "db": {
                        "status": {
                            "valid": True,
                            "built": "2026-11-08T00:00:00Z",
                            "schemaVersion": "v6",
                            "from": "test-database",
                        }
                    }
                },
                "matches": [],
            },
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(preparation, "run", run)
    monkeypatch.setattr(preparation.subprocess, "run", scan)
    assert preparation.assess(tmp_path, IMAGE, require_clean=require_clean)["outcome"] == "clean"
    assert commands == [["syft", "scan"], ["syft", "convert"], ["grype", "db"]]
    assert preparation.read(tmp_path / "sbom.raw.syft.json") == inventory
