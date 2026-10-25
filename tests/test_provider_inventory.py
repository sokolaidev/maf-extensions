"""Provider coverage must come from the approved manifest, including every baked version."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import image_security_evidence as evidence  # noqa: E402

pytestmark = pytest.mark.workflow
IMAGE_ID = "sha256:" + "a" * 64


def provider(version="3.9.1", registry="registry.terraform.io"):
    return {
        "source": f"{registry}/hashicorp/random",
        "version": version,
        "platform": "linux_amd64",
        "source_build": {"go_version": "go1.27.2"},
    }


def components(item, suffix=None):
    name = item["source"].rsplit("/", 1)[-1]
    version = item["version"]
    guest_path = (
        f"/opt/maf-terraform/mirror/{item['source']}/{version}/{item['platform']}/"
        f"terraform-provider-{name}{suffix if suffix is not None else '_v' + version}"
    )
    module = f"github.com/terraform-providers/terraform-provider-{name}"
    return [
        {
            "type": "go-module",
            "name": module,
            "version": "UNKNOWN",
            "metadata": {"mainModule": module},
            "locations": [{"path": guest_path}],
        },
        {
            "type": "go-module",
            "name": "stdlib",
            "version": item.get("source_build", {}).get("go_version", "go1.27.2"),
            "locations": [{"path": guest_path}],
        },
    ]


def sbom(artifacts):
    return {
        "source": {"type": "image", "metadata": {"imageID": IMAGE_ID}},
        "artifacts": artifacts,
    }


@pytest.mark.parametrize("suffix", ["", "_v3.9.1", "_v3.9.1_x5"])
def test_provider_binary_naming_variants_are_inventoried(suffix):
    evidence.verify_providers(sbom(components(provider(), suffix)), {"providers": [provider()]})


@pytest.mark.parametrize(
    "defect",
    [
        "engine-only",
        "zip-only",
        "wrong-registry",
        "wrong-version",
        "wrong-platform",
        "wrong-provider",
        "nested-path",
        "no-main-module",
        "no-stdlib",
        "unknown-stdlib",
        "old-go",
        "non-go",
        "conflicting-go",
        "duplicate-binaries",
    ],
)
def test_missing_or_misattributed_provider_components_fail(defect):
    artifacts = components(provider())
    if defect in {
        "engine-only",
        "zip-only",
        "wrong-registry",
        "wrong-version",
        "wrong-platform",
        "wrong-provider",
        "nested-path",
    }:
        guest_path = artifacts[0]["locations"][0]["path"]
        changed = {
            "engine-only": "/usr/local/bin/terraform",
            "zip-only": guest_path + ".zip",
            "wrong-registry": guest_path.replace("registry.terraform.io", "registry.opentofu.org"),
            "wrong-version": guest_path.replace("3.9.1", "3.9.0"),
            "wrong-platform": guest_path.replace("linux_amd64", "linux_arm64"),
            "wrong-provider": guest_path.replace(
                "terraform-provider-random", "terraform-provider-other"
            ),
            "nested-path": guest_path + "/unpacked",
        }[defect]
        for artifact in artifacts:
            artifact["locations"][0]["path"] = changed
    elif defect == "no-main-module":
        artifacts[0]["name"] = "golang.org/x/net"
    elif defect == "no-stdlib":
        artifacts.pop()
    elif defect in {"unknown-stdlib", "old-go"}:
        artifacts[1]["version"] = "UNKNOWN" if defect == "unknown-stdlib" else "go1.26.8"
    elif defect == "non-go":
        for artifact in artifacts:
            artifact["type"] = "binary"
    elif defect == "conflicting-go":
        artifacts.append(copy.deepcopy(artifacts[1]) | {"version": "go1.26.8"})
    elif defect == "duplicate-binaries":
        artifacts.extend(components(provider(), "_v3.9.1_x5"))
    with pytest.raises(ValueError, match="Provider"):
        evidence.verify_providers(sbom(artifacts), {"providers": [provider()]})


def test_every_manifest_version_needs_its_own_inventory():
    providers = [provider(), provider("3.9.2")]
    artifacts = components(providers[0])
    with pytest.raises(ValueError, match="3.9.2"):
        evidence.verify_providers(sbom(artifacts), {"providers": providers})
    artifacts.extend(components(providers[1]))
    evidence.verify_providers(sbom(artifacts), {"providers": providers})


@pytest.mark.parametrize("providers", [[], None, {}])
def test_empty_manifest_cannot_disable_provider_coverage(providers):
    with pytest.raises(ValueError, match="Expected provider inventory"):
        evidence.verify_providers(sbom(components(provider())), {"providers": providers})


@pytest.mark.parametrize(
    "profile", ["terraform-random", "opentofu-random", "terraform-avm", "opentofu-avm"]
)
def test_cli_requires_every_provider_from_the_selected_manifest(tmp_path, monkeypatch, profile):
    manifest = json.loads(
        (ROOT / "images/terraform-sandbox" / evidence.PROVIDER_MANIFESTS[profile]).read_text()
    )
    artifacts = [component for item in manifest["providers"] for component in components(item)]
    (tmp_path / "build.json").write_text(
        json.dumps({"local_image_id": IMAGE_ID, "profile": profile})
    )
    inventory = tmp_path / "sbom.syft.json"
    inventory.write_text(json.dumps(sbom(artifacts)))
    monkeypatch.setattr(
        sys, "argv", ["image_security_evidence.py", str(tmp_path), "--verify-inventory"]
    )
    evidence.main()
    inventory.write_text(json.dumps(sbom([{"name": "libc"}, *artifacts[:-2]])))
    with pytest.raises(ValueError, match="Provider inventory"):
        evidence.main()


def test_profiles_without_providers_keep_their_existing_inventory_contract():
    evidence.verify_profile_inventory(sbom([{"name": "libc"}]), "bicep")
