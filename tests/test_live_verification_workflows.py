"""New live coverage and release dispatch must select the same exercised packages."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parents[1]


def selected(condition):
    match = re.search(r"fromJSON\('([^']+)'\)", condition)
    assert match
    return set(json.loads(match[1]))


def test_release_filters_include_otel_shell_and_compaction_pair():
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-packages.yml").read_text("utf-8"))
    filters = [
        selected(workflow["jobs"][job]["if"])
        for job in ("wait-for-propagation", "train-status", "verify")
    ]
    assert filters[0] == filters[1] == filters[2]
    assert {"maf-sandbox-otel", "maf-sandbox-shell", "maf-compaction", "maf-cachebench"} <= filters[
        0
    ]
    assert (
        not {
            "maf-sandbox-bubblewrap",
            "maf-sandbox-mcp",
            "maf-sandbox-tui",
            "maf-sandbox-hyperlight",
        }
        & filters[0]
    )


@pytest.mark.parametrize(
    "job,packages,checker",
    [
        (
            "sample-21",
            {"maf-sandbox", "maf-sandbox-docker", "maf-sandbox-shell"},
            "check_live_shell_sample.py",
        ),
        ("sample-22", {"maf-compaction", "maf-cachebench"}, "check_live_compaction_sample.py"),
    ],
)
def test_new_jobs_are_bounded_and_check_measured_output(job, packages, checker):
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    definition = workflow["jobs"][job]
    assert selected(definition["if"]) == packages
    assert "inputs.package == ''" in definition["if"]
    assert definition["timeout-minutes"] <= (55 if job == "sample-21" else 25)
    assert definition["environment"] == "live-verify"
    steps = definition["steps"]
    run = next(step["run"] for step in steps if checker in step.get("run", ""))
    assert "set -euo pipefail" in run and "| tee" in run
    assert '"$HARNESS"/scripts/' + checker in run
    assert any(
        step.get("if") == "always()" and "upload-artifact@" in step.get("uses", "")
        for step in steps
    )


def test_shell_acas_checks_published_packages_and_preserves_failure_evidence():
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    job = workflow["jobs"]["shell-acas"]
    assert selected(job["if"]) == {"maf-sandbox", "maf-sandbox-acas", "maf-sandbox-shell"}
    assert "needs" not in job
    assert job["environment"] == "live-verify"
    assert job["timeout-minutes"] == "${{ inputs.import_shell_image && 55 || 25 }}"
    steps = job["steps"]
    run = next(step["run"] for step in steps if "qualify_shell_acas.py" in step.get("run", ""))
    assert 'uv run --no-project "${source_args[@]}" "$HARNESS"/scripts/qualify_shell_acas.py' in run
    assert 'if [ "$SOURCE" = branch ]' in run
    assert "set -euo pipefail" in run and "| tee" in run
    version = next(step for step in steps if "check_live_versions.py" in step.get("run", ""))
    assert "inputs.source != 'branch'" in version["if"]
    artifact = next(step for step in steps if "upload-artifact@" in step.get("uses", ""))
    assert artifact["if"] == "always()"
    assert "shell-acas.json" in artifact["with"]["path"]


def test_shell_import_is_explicit_manual_setup_and_precedes_the_read_only_check():
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["import_shell_image"]["default"] is False
    assert "import_shell_image" not in triggers["workflow_call"]["inputs"]
    assert workflow["jobs"]["acas-images"]["timeout-minutes"] == 10
    steps = workflow["jobs"]["shell-acas"]["steps"]
    importer = next(
        i for i, step in enumerate(steps) if "import_disk_image.py" in step.get("run", "")
    )
    checker = next(
        i for i, step in enumerate(steps) if "check_acas_live_images.py" in step.get("run", "")
    )
    assert importer < checker
    assert steps[importer]["timeout-minutes"] == 30
    assert steps[checker]["timeout-minutes"] == 10
    assert "--package maf-sandbox-shell" in steps[checker]["run"]
    assert "continue-on-error" not in steps[checker]
    assert steps[importer]["if"] == (
        "github.event_name == 'workflow_dispatch' && inputs.import_shell_image "
        "&& inputs.package == 'maf-sandbox-shell'"
    )
    assert 'image_reference(os.environ["MAF_SHELL_ACAS_IMAGE"])' in steps[importer]["run"]


def test_shell_setup_is_isolated_from_shared_and_daily_prerequisites():
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    shared = workflow["jobs"]["acas-images"]
    shell = workflow["jobs"]["shell-acas"]
    assert "maf-sandbox-shell" not in selected(shared["if"])
    assert "needs" not in shell
    assert "MAF_SHELL_ACAS_IMAGE" not in shared["env"]
    assert all("import_disk_image.py" not in step.get("run", "") for step in shared["steps"])
    harness = next(
        step for step in shell["steps"] if step.get("with", {}).get("path") == ".harness"
    )
    assert "sparse-checkout" not in harness["with"]
    daily = yaml.safe_load((ROOT / ".github/workflows/conformance-live.yml").read_text("utf-8"))
    for step in daily["jobs"]["acas-conformance"]["steps"]:
        assert "MAF_SHELL_ACAS_IMAGE" not in step.get("env", {})
