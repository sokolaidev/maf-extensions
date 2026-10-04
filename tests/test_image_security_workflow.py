"""Image evidence must identify the scanned bytes and fail visibly on incomplete checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from image_security_evidence import record, verify_inventory  # noqa: E402

pytestmark = pytest.mark.workflow
IMAGE_ID = "sha256:" + "a" * 64
REVISION = "b" * 40


def image():
    return {"Id": IMAGE_ID, "Os": "linux", "Architecture": "amd64"}


@pytest.mark.parametrize(
    "details,revision",
    [
        ([], REVISION),
        ([image(), image()], REVISION),
        ([image() | {"Id": "example:latest"}], REVISION),
        ([image() | {"Id": "sha256:short"}], REVISION),
        ([image() | {"Architecture": "arm64"}], REVISION),
        ([image() | {"Os": "windows"}], REVISION),
        ([image()], "main"),
    ],
)
def test_evidence_refuses_an_unidentified_or_uncovered_target(details, revision):
    with pytest.raises(ValueError):
        record(details, revision, "bicep")


def test_workflow_output_identifies_the_same_image_as_the_retained_record(tmp_path):
    (tmp_path / "image-inspect.json").write_text(json.dumps([image()]))
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/image_security_evidence.py"), str(tmp_path)],
        cwd=ROOT,
        env=os.environ
        | {
            "PROFILE": "bicep",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "sokolaidev/maf-extensions",
            "GITHUB_RUN_ID": "123",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        check=True,
    )
    evidence = json.loads((tmp_path / "build.json").read_text())
    assert output.read_text() == f"image_id={evidence['local_image_id']}\n"
    assert evidence["local_image_id"] == IMAGE_ID
    assert (
        evidence["source_commit"]
        == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )
    assert "not a registry manifest digest" in summary.read_text()
    assert evidence["run_url"] == "https://github.com/sokolaidev/maf-extensions/actions/runs/123"


@pytest.mark.parametrize("artifacts", [[], None, {}, "component"])
def test_empty_or_malformed_inventory_cannot_count_as_clean(artifacts):
    with pytest.raises(ValueError):
        verify_inventory({"artifacts": artifacts}, IMAGE_ID)


@pytest.mark.parametrize("source_type,image_id", [("directory", IMAGE_ID), ("image", "other")])
def test_inventory_cannot_describe_another_source(source_type, image_id):
    with pytest.raises(ValueError):
        verify_inventory(
            {
                "artifacts": [{"name": "libc"}],
                "source": {"type": source_type, "metadata": {"imageID": image_id}},
            },
            IMAGE_ID,
        )


def test_matching_image_inventory_is_accepted():
    verify_inventory(
        {
            "artifacts": [{"name": "libc"}],
            "source": {"type": "image", "metadata": {"imageID": IMAGE_ID}},
        },
        IMAGE_ID,
    )


def test_scans_fail_on_unfixed_high_findings_and_keep_failure_evidence():
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["scan"]
    assert job["strategy"]["fail-fast"] is False
    assert "continue-on-error" not in job
    steps = job["steps"]
    assert not any("continue-on-error" in step for step in steps)
    inventory = next(s for s in steps if s.get("uses", "").startswith("anchore/sbom-action@"))
    scan = next(s for s in steps if s.get("uses", "").startswith("anchore/scan-action@"))
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert inventory["with"]["image"] == "docker:${{ steps.identity.outputs.image_id }}"
    assert scan["with"]["sbom"] == inventory["with"]["output-file"]
    assert scan["with"]["severity-cutoff"] == "high"
    assert scan["with"]["fail-build"] is True
    assert scan["with"]["only-fixed"] is False
    assert scan["with"]["config"]
    assert upload["if"] == "always() && steps.identity.outcome == 'success'"
    assert upload["with"]["if-no-files-found"] == "error"
    assert inventory["with"]["upload-release-assets"] is False


def test_every_scan_profile_is_named_in_the_documented_scope():
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    profiles = workflow["jobs"]["scan"]["strategy"]["matrix"]["include"]
    names = [p["profile"] for p in profiles]
    assert len(names) == len(set(names)) == 12
    scope = (ROOT / "docs/security/container-images.md").read_text()
    for profile in names:
        assert f"| `{profile}` |" in scope
