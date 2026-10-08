"""Candidate preparation refuses identity drift and cleans up failed runtime probes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_container_release_image import check  # noqa: E402
from prepare_container_release import labels, require_source  # noqa: E402


def test_release_labels_do_not_replace_upstream_version():
    result = labels("bicep", "0.1.0", "a" * 40)
    assert "org.opencontainers.image.version" not in result
    assert result["dev.sokolai.maf.image.version"] == "0.1.0"


@pytest.mark.parametrize(
    "changed",
    [
        {"GITHUB_REF": "refs/heads/feature"},
        {"GITHUB_REPOSITORY": "someone/fork"},
        {"GITHUB_SHA": "b" * 40},
    ],
)
def test_source_cannot_be_selected_independently_of_dispatch(monkeypatch, changed):
    environment = {
        "GITHUB_REPOSITORY": "sokolaidev/maf-extensions",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
    } | changed
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "prepare_container_release.run", lambda *a, **k: pytest.fail("must reject before building")
    )
    with pytest.raises(ValueError):
        require_source("a" * 40)


@pytest.mark.parametrize(
    "profile",
    [
        "bicep",
        "bicep-prepared",
        "sbx-bicep",
        "graphviz",
        "drawio-sandbox",
        "drawio-export",
        "terraform-random",
        "opentofu-random",
        "terraform-prepared",
        "opentofu-prepared",
        "egress-proxy",
    ],
)
def test_runtime_probe_is_offline_bounded_and_removed_on_failure(monkeypatch, tmp_path, profile):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["check"]
        return subprocess.CompletedProcess(command, 0)

    async def fail(command):
        raise TimeoutError("probe did not exit")

    monkeypatch.setattr("check_container_release_image.subprocess.run", run)
    monkeypatch.setattr("check_container_release_image._smoke_output", fail)
    with pytest.raises(TimeoutError):
        check(profile, "sha256:" + "a" * 64, tmp_path)
    create, cleanup = commands
    assert create[create.index("--network") + 1] == "none"
    assert "--read-only" in create and "--pids-limit" in create and "--memory" in create
    assert create[create.index("--cap-drop") + 1] == "ALL"
    assert cleanup == ["docker", "rm", "--force", create[create.index("--name") + 1]]
    if profile == "drawio-export":
        compile(create[-1], "drawio-export-probe", "exec")
        grants = {create[i + 1] for i, value in enumerate(create) if value == "--cap-add"}
        assert grants == {"CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"}
    else:
        assert "--cap-add" not in create
    if profile.startswith(("terraform-", "opentofu-")):
        compile(create[-1], "terraform-probe", "exec")


def test_mutable_runtime_target_is_rejected_before_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "check_container_release_image.subprocess.run",
        lambda *a, **k: pytest.fail("no mutable image execution"),
    )
    with pytest.raises(ValueError):
        check("bicep", "bicep:latest", tmp_path)
