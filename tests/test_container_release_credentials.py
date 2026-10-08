"""Keep workflow-writing credentials out of scans, signatures and ordinary API requests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from container_release_history import GitHub  # noqa: E402

pytestmark = pytest.mark.workflow
RELEASES = "repos/sokolaidev/maf-extensions/releases"


@pytest.mark.parametrize(
    "method,endpoint,uses_app",
    [
        ("POST", RELEASES, True),
        ("PATCH", RELEASES + "/123", True),
        ("GET", RELEASES, False),
        ("GET", RELEASES + "/123", False),
        ("POST", RELEASES + "/123/assets", False),
        ("POST", "https://uploads.github.com/" + RELEASES + "/123/assets?name=test", False),
        ("POST", "repos/other/repository/releases", False),
        ("POST", "repos/sokolaidev/maf-extensions/git/refs", False),
        ("POST", "https://example.invalid/" + RELEASES, False),
        ("DELETE", RELEASES + "/123", False),
    ],
)
def test_app_token_is_scoped_to_fixed_repository_release_metadata(
    monkeypatch, method, endpoint, uses_app
):
    monkeypatch.setenv("GH_TOKEN", "job-token")
    monkeypatch.setenv("CONTAINER_RELEASE_TOKEN", "app-token")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"{}")

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    GitHub().request(endpoint, method=method)
    command, kwargs = seen[0]
    assert kwargs["env"]["GH_TOKEN"] == ("app-token" if uses_app else "job-token")
    assert "CONTAINER_RELEASE_TOKEN" not in kwargs["env"]
    assert not any("app-token" in argument for argument in command)
    assert os.environ["GH_TOKEN"] == "job-token"


@pytest.mark.parametrize("method,endpoint", [("POST", RELEASES), ("PATCH", RELEASES + "/123")])
def test_actions_cannot_fall_back_to_job_token_for_release_metadata(monkeypatch, method, endpoint):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GH_TOKEN", "job-token")
    monkeypatch.delenv("CONTAINER_RELEASE_TOKEN", raising=False)
    monkeypatch.setattr(
        "container_release_history.subprocess.run",
        lambda *a, **k: pytest.fail("missing App token must refuse before any API write"),
    )
    with pytest.raises(ValueError, match="App token"):
        GitHub().request(endpoint, method=method)


def test_failed_app_write_is_not_retried_with_another_identity(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GH_TOKEN", "job-token")
    monkeypatch.setenv("CONTAINER_RELEASE_TOKEN", "app-token")
    tokens = []

    def run(command, **kwargs):
        tokens.append(kwargs["env"]["GH_TOKEN"])
        raise subprocess.CalledProcessError(1, command, stderr=b"gh: Forbidden (HTTP 403)")

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    with pytest.raises(subprocess.CalledProcessError):
        GitHub().request(RELEASES, method="POST")
    assert tokens == ["app-token"]


@pytest.mark.parametrize(
    "filename,writers",
    [
        (
            "container-image-release.yml",
            {"reserve", "complete", "deliver", "abandon", "retire-lost-candidate"},
        ),
        ("container-image-monitor.yml", {"begin", "finish"}),
        ("container-image-recover.yml", {"write"}),
    ],
)
def test_only_catalogue_writers_receive_repository_scoped_app_tokens(filename, writers):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
    assert workflow["env"]["GH_TOKEN"] == "${{ github.token }}"
    for name, job in workflow["jobs"].items():
        steps = job["steps"]
        minted = [
            step
            for step in steps
            if step.get("uses", "").startswith("actions/create-github-app-token@")
        ]
        consumers = [step for step in steps if "CONTAINER_RELEASE_TOKEN" in step.get("env", {})]
        if name not in writers:
            assert not minted and not consumers
            continue
        assert len(minted) == len(consumers) == 1
        token = minted[0]
        assert token["with"] == {
            "client-id": "${{ vars.CONTAINER_RELEASE_APP_CLIENT_ID }}",
            "private-key": "${{ secrets.CONTAINER_RELEASE_APP_PRIVATE_KEY }}",
            "owner": "${{ github.repository_owner }}",
            "repositories": "maf-extensions",
            "permission-contents": "write",
            "permission-workflows": "write",
        }
        assert token["id"] == "release-token"
        assert steps.index(token) < steps.index(consumers[0])
        assert (
            consumers[0]["env"]["CONTAINER_RELEASE_TOKEN"]
            == "${{ steps.release-token.outputs.token }}"
        )
        assert "GH_TOKEN" not in consumers[0]["env"]


def test_release_policy_refuses_missing_app_configuration_before_prepare():
    workflow = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    policy = next(
        step
        for step in workflow["jobs"]["policy"]["steps"]
        if "RELEASE_APP_CONFIGURED" in step.get("env", {})
    )
    assert "vars.CONTAINER_RELEASE_APP_CLIENT_ID != ''" in policy["env"]["RELEASE_APP_CONFIGURED"]
    assert (
        "secrets.CONTAINER_RELEASE_APP_PRIVATE_KEY != ''" in policy["env"]["RELEASE_APP_CONFIGURED"]
    )
    assert 'if [ "$RELEASE_APP_CONFIGURED" != true ]; then' in policy["run"]
    assert "exit 1" in policy["run"]
    assert workflow["jobs"]["prepare"]["needs"] == "policy"


def test_recovery_keeps_protected_approval_and_both_writer_locks():
    workflow = yaml.safe_load((ROOT / ".github/workflows/container-image-recover.yml").read_text())
    assert workflow["concurrency"]["group"] == "container-image-publication"
    writer = workflow["jobs"]["write"]
    assert writer["environment"] == "container-release"
    assert writer["concurrency"]["group"] == "container-security-catalogue"
    assert writer["permissions"] == {"contents": "write", "actions": "read"}
    assert "id-token" not in str(workflow) and "actions/attest@" not in str(workflow)
    assert "github.ref == 'refs/heads/main'" in workflow["jobs"]["inspect"]["if"]
