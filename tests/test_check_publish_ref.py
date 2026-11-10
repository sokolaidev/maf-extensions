"""PyPI uploads must carry the release tag in their workflow identity."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from _workflow_commands import PUBLISH_WORKFLOW, ROOT, command_arguments

pytestmark = pytest.mark.workflow

WORKFLOW = yaml.safe_load(PUBLISH_WORKFLOW.read_text("utf-8"))
BUILD = WORKFLOW["jobs"]["build"]["steps"]
PUBLISH = WORKFLOW["jobs"]["publish"]["steps"]
RESOLVE = next(step for step in BUILD if step.get("id") == "resolve")
UPLOAD_GUARD = next(
    step for step in PUBLISH if step.get("name") == "Check the workflow ref before upload"
)


@pytest.mark.parametrize("step", [RESOLVE, UPLOAD_GUARD], ids=["resolve", "upload"])
@pytest.mark.parametrize(
    ("target", "ref", "accepted"),
    [
        ("pypi", "refs/tags/maf-sandbox-document-v0.4.1", True),
        ("pypi", "refs/heads/main", False),
        ("pypi", "refs/heads/maf-sandbox-document-v0.4.1", False),
        ("pypi", "refs/tags/maf-sandbox-document-v0.4.0", False),
        ("pypi", "refs/tags/maf-sandbox-docker-v0.4.1", False),
        ("pypi", "maf-sandbox-document-v0.4.1", False),
        ("pypi", "", False),
        ("testpypi", "refs/heads/main", True),
        ("testpypi", "refs/tags/maf-sandbox-document-v0.4.1", True),
        ("other", "refs/tags/maf-sandbox-document-v0.4.1", False),
        ("", "refs/tags/maf-sandbox-document-v0.4.1", False),
    ],
)
def test_workflow_guard(step: dict, target: str, ref: str, accepted: bool):
    variables = {
        "target": target,
        "package": "maf-sandbox-document",
        "version": "0.4.1",
        "WORKFLOW_REF": ref,
    }
    variables.update({key.upper(): value for key, value in variables.copy().items()})
    arguments = command_arguments(step["run"], "check_publish_ref.py", variables)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_publish_ref.py"), *arguments],
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    if target == "pypi" and not accepted:
        assert "refs/tags/maf-sandbox-document-v0.4.1" in result.stdout
        assert "Re-run the failed release-tag workflow" in result.stdout
        assert "Branch dispatches must target testpypi" in result.stdout


def test_guards_use_full_workflow_identity_and_fail_before_upload():
    for step in (RESOLVE, UPLOAD_GUARD):
        assert step["env"]["WORKFLOW_REF"] == "${{ github.ref }}"
        assert "if" not in step
        assert not step.get("continue-on-error")
    assert "set -euo pipefail" in RESOLVE["run"]
    assert RESOLVE["run"].index("check_publish_ref.py") < RESOLVE["run"].index('echo "package=')
    assert UPLOAD_GUARD["env"] == {
        "TARGET": "${{ needs.build.outputs.target }}",
        "PACKAGE": "${{ needs.build.outputs.package }}",
        "VERSION": "${{ needs.build.outputs.version }}",
        "WORKFLOW_REF": "${{ github.ref }}",
    }
    upload = next(
        step for step in PUBLISH if "pypa/gh-action-pypi-publish@" in step.get("uses", "")
    )
    assert PUBLISH.index(UPLOAD_GUARD) + 1 == PUBLISH.index(upload)
    assert "build" in WORKFLOW["jobs"]["publish"]["needs"]


@pytest.mark.skipif(
    sys.platform != "linux", reason="production Bash integration runs only on Linux"
)
@pytest.mark.parametrize(
    ("event", "target", "ref", "accepted"),
    [
        ("workflow_dispatch", "pypi", "refs/heads/main", False),
        ("workflow_dispatch", "pypi", "refs/heads/maf-sandbox-document-v0.4.1", False),
        ("workflow_dispatch", "pypi", "refs/tags/maf-sandbox-document-v0.4.1", True),
        ("workflow_dispatch", "pypi", "refs/tags/maf-sandbox-document-v0.4.0", False),
        ("workflow_dispatch", "testpypi", "refs/heads/main", True),
        ("push", "", "refs/tags/maf-sandbox-document-v0.4.1", True),
    ],
)
def test_resolve_shell(tmp_path: Path, event: str, target: str, ref: str, accepted: bool):
    package = tmp_path / "packages/maf-sandbox-document"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text('version = "0.4.1"\n', encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check_publish_ref.py").write_bytes(
        (ROOT / "scripts/check_publish_ref.py").read_bytes()
    )
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", RESOLVE["run"]],
        cwd=tmp_path,
        env=os.environ
        | {
            "EVENT_NAME": event,
            "REF_NAME": ref.removeprefix("refs/heads/").removeprefix("refs/tags/"),
            "WORKFLOW_REF": ref,
            "INPUT_PACKAGE": "maf-sandbox-document",
            "INPUT_TARGET": target,
            "INPUT_VERSION": "",
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    if accepted:
        assert "version=0.4.1" in output.read_text("utf-8")
    else:
        assert not output.exists()
