"""Shared preparation must fail closed without reusing security verdicts or release evidence."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.workflow


def test_cache_is_exact_daily_and_only_written_by_default_branch():
    action = yaml.safe_load(
        (ROOT / ".github/actions/prepare-wolfi-python/action.yml").read_text("utf-8")
    )
    steps = action["runs"]["steps"]
    restore = next(s for s in steps if s.get("uses", "").startswith("actions/cache/restore@"))
    save = next(s for s in steps if s.get("uses", "").startswith("actions/cache/save@"))
    assert restore["if"] == "inputs.fresh != 'true'"
    assert "--bundle-key" in steps[0]["run"]
    assert "restore-keys" not in restore["with"]
    assert restore["with"]["key"] == save["with"]["key"]
    assert "steps.inputs.outputs.key" in save["with"]["key"]
    assert "github.event.repository.default_branch" in save["if"]
    assert "github.event_name != 'pull_request'" in save["if"]
    verify = next(s for s in steps if "--verify-bundle" in s.get("run", ""))
    assert "if" not in verify and not verify.get("continue-on-error")
    assert steps.index(verify) < steps.index(save)


def test_matrix_uses_successful_producer_artifact_including_failed_job_retries():
    jobs = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text("utf-8"))[
        "jobs"
    ]
    producer = jobs["python-packages"]
    assert producer["if"] == "needs.select.outputs.python == 'true'"
    prepare = next(s for s in producer["steps"] if s.get("uses", "").startswith("./"))
    assert prepare["with"]["fresh"] == "${{ github.event_name != 'pull_request' }}"
    upload = next(s for s in producer["steps"] if s.get("id") == "bundle")
    assert "github.run_id" in upload["with"]["name"]
    assert "github.run_attempt" in upload["with"]["name"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert producer["outputs"]["artifact"] == "${{ steps.bundle.outputs.artifact-id }}"
    scan = jobs["scan"]
    download = next(
        s for s in scan["steps"] if s.get("name") == "Download the shared Python preparation"
    )
    assert download["with"]["artifact-ids"] == "${{ needs.python-packages.outputs.artifact }}"
    assert download["with"]["merge-multiple"] is True
    verify = next(s for s in scan["steps"] if "--verify-bundle" in s.get("run", ""))
    assert verify["if"] == download["if"]
    assert "needs.select.outputs.python_profiles" in verify["if"]
    assert "MAF_WOLFI_PYTHON_PACKAGES=" in verify["run"]
    assert not verify.get("continue-on-error")
    build = next(s for s in scan["steps"] if s.get("id") == "build")
    assert scan["steps"].index(verify) < scan["steps"].index(build)
    assert not any("--prepare-bundle" in s.get("run", "") for s in scan["steps"])


def test_release_workflows_do_not_inherit_pr_preparation():
    for path in (ROOT / ".github/workflows").glob("*release*.yml"):
        text = path.read_text("utf-8")
        assert "prepare-wolfi-python" not in text
        assert "MAF_WOLFI_PYTHON_PACKAGES" not in text


def test_hyperlight_image_selection_is_separate_from_code_qualification():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text("utf-8"))["jobs"]
    changes = jobs["changes"]
    assert changes["outputs"]["hyperlight"] == "${{ steps.images.outputs.hyperlight }}"
    selector = next(s for s in changes["steps"] if s.get("id") == "images")
    assert selector["run"] == "python3 scripts/select_image_security.py"
    steps = jobs["hyperlight-linux-worker"]["steps"]
    prepare = next(s for s in steps if s.get("id") == "python-packages")
    assert "needs.changes.outputs.hyperlight == 'true'" in prepare["if"]
    assert prepare["with"]["fresh"] == "${{ github.event_name != 'pull_request' }}"
    build = next(s for s in steps if "scripts/build_hyperlight_aks_image.py" in s.get("run", ""))
    assert (
        build["env"]["MAF_WOLFI_PYTHON_PACKAGES"]
        == "${{ steps.python-packages.outputs.directory }}"
    )
