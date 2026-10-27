"""Replacement scans and optional diagnostics keep distinct Bicep compiler routes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from image_scan_policy import BICEP_ROUTES, evidence_route, scan_route  # noqa: E402
from select_image_security import PROFILES  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("requested", ["policy", "source", "upstream"])
def test_scan_routes_preserve_non_bicep_profiles(profile, requested):
    expected = (
        ("source" if requested == "policy" else requested)
        if profile in BICEP_ROUTES
        else "upstream"
    )
    assert scan_route(profile, requested) == expected


@pytest.mark.parametrize("profile,route", [("unknown", "policy"), ("bicep", "unknown")])
def test_unknown_policy_selection_is_refused(profile, route):
    with pytest.raises(ValueError, match="Unknown"):
        scan_route(profile, route)


@pytest.mark.parametrize("route,purpose", [("policy", "replacement"), ("upstream", "diagnostic")])
def test_selected_route_reaches_the_build_and_qualification(tmp_path, route, purpose):
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    steps = workflow["jobs"]["scan"]["steps"]
    selection = next(step for step in steps if step.get("id") == "route")
    assert selection["env"]["REQUESTED_BICEP_ROUTE"] == "${{ inputs.bicep_route || 'policy' }}"
    build = next(step for step in steps if step.get("id") == "build")
    qualify = next(
        step for step in steps if step.get("name") == "Qualify the selected Bicep compiler"
    )
    assert steps.index(selection) < steps.index(build) < steps.index(qualify)
    for step in (build, qualify):
        assert step["env"]["BICEP_ROUTE"] == "${{ steps.route.outputs.bicep_route }}"
    output, summary = tmp_path / "output", tmp_path / "summary"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/image_scan_policy.py")],
        check=True,
        env=os.environ
        | {
            "PROFILE": "bicep-docker-sbx",
            "REQUESTED_BICEP_ROUTE": route,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )
    assert output.read_text().strip() == f"bicep_route={scan_route('bicep-docker-sbx', route)}"
    assert f"**{purpose}**" in summary.read_text()


@pytest.mark.parametrize("profile", BICEP_ROUTES)
@pytest.mark.parametrize("mutation", [None, "image", "profile", "route", "inventory"])
def test_route_evidence_requires_matching_runtime_and_patched_inventory(profile, mutation):
    from bicep_source import policy

    packages = policy(ROOT / "images/bicep-sandbox/source.json")["packages"]
    build = {"route": "source", "packages": packages}
    runtime = {
        "profile": profile,
        "imageId": "image",
        "bicepAvm" if profile == "bicep-avm" else "bicep": {
            "build": build,
        },
    }
    sbom = {
        "artifacts": [
            {"type": "dotnet", "name": name, "version": version}
            for name, version in packages.items()
        ]
    }
    if mutation in {"image", "profile"}:
        runtime["imageId" if mutation == "image" else "profile"] = "other"
    elif mutation == "route":
        build.pop("route")
    elif mutation == "inventory":
        sbom["artifacts"].pop()
    if mutation:
        with pytest.raises(ValueError):
            evidence_route(profile, "image", runtime, sbom)
    else:
        assert evidence_route(profile, "image", runtime, sbom) == "source"
