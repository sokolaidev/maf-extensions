"""Candidate preparation refuses identity drift and cleans up failed runtime probes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_container_release as preparation  # noqa: E402
from check_container_release_image import check  # noqa: E402


def test_release_labels_do_not_replace_upstream_version():
    result = preparation.labels("bicep", "0.1.0", "a" * 40)
    assert "org.opencontainers.image.version" not in result
    assert result["dev.sokolai.maf.image.version"] == "0.1.0"


@pytest.mark.parametrize("require_clean", [False, True])
@pytest.mark.parametrize("fixed", [False, True])
def test_assessment_reports_blocking_packages_and_retains_evidence(
    monkeypatch, tmp_path, require_clean, fixed
):
    image_id = "sha256:" + "a" * 64
    report = {
        "source": {"type": "image", "target": {"imageID": image_id}},
        "descriptor": {
            "db": {
                "status": {
                    "valid": True,
                    "built": "2026-01-01T00:00:00Z",
                    "schemaVersion": "v6",
                    "from": "test-database",
                }
            }
        },
        "matches": [
            {
                "vulnerability": {
                    "id": "CVE-example",
                    "severity": "Critical",
                    "fix": {"state": "fixed", "versions": ["1.2.4-r0"]}
                    if fixed
                    else {"state": "not-fixed"},
                },
                "artifact": {"name": "python-test", "version": "1.2.3-r0", "type": "apk"},
            }
        ],
    }
    preparation.write(tmp_path / "grype.json", report)
    preparation.write(tmp_path / "sbom.syft.json", {})
    monkeypatch.setattr(preparation, "verify_inventory", lambda *a: None)
    monkeypatch.setattr(preparation, "run", lambda *a, **kw: None)
    monkeypatch.setattr(
        preparation.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 2)
    )
    if require_clean:
        with pytest.raises(ValueError, match="High/Critical assessment failed") as caught:
            preparation.assess(tmp_path, image_id)
        message = str(caught.value)
        for detail in ("Critical", "CVE-example", "python-test", "1.2.3-r0", "apk", "grype.json"):
            assert detail in message
        assert ("1.2.4-r0" if fixed else "not-fixed") in message
    else:
        assert (
            preparation.assess(tmp_path, image_id, require_clean=False)["outcome"] == "vulnerable"
        )
    assert preparation.read(tmp_path / "grype.json") == report
    assert preparation.read(tmp_path / "assessment.json")["outcome"] == "vulnerable"


def test_failure_diagnostics_bound_output_and_escape_untrusted_fields():
    finding = {
        "vulnerability": {"id": "CVE-test\n::warning::injected", "severity": "High"},
        "artifact": {"name": "test\npackage"},
    }
    message = preparation.assessment_failure({"matches": [finding] * 22})
    assert message.count("  Finding:") == 20
    assert "2 additional blocking findings omitted" in message
    assert "\\n::warning::injected" in message
    assert "\n::warning::" not in message
    assert '"installed": "unknown"' in message


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
        preparation.require_source("a" * 40)


@pytest.mark.parametrize(
    "profile",
    [
        "bicep",
        "bicep-avm",
        "graphviz",
        "terraform-random",
        "opentofu-random",
        "terraform-avm",
        "opentofu-avm",
    ],
)
def test_runtime_probe_is_offline_bounded_and_removed_on_failure(monkeypatch, tmp_path, profile):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["check"]
        return subprocess.CompletedProcess(command, 0)

    async def fail(command, **kwargs):
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


@pytest.mark.parametrize("profile", preparation.profiles())
def test_release_build_budget_includes_python_compilation(monkeypatch, tmp_path, profile):
    monkeypatch.setattr(preparation, "require_source", lambda *args: None)
    monkeypatch.setattr(preparation, "preflight", lambda *args: None)
    python_profiles = {
        "document",
        "drawio",
        "drawio-export",
        "shell",
        "terraform-random",
        "opentofu-random",
        "terraform-avm",
        "opentofu-avm",
        "hyperlight-aks-probe",
    }
    expected = 7200 if profile == "terraform-avm" else 2400
    if profile in python_profiles:
        expected += 1800

    def stop_at_build(command, **kwargs):
        assert command == ["bash", "scripts/build_scan_image.sh", profile]
        assert kwargs["timeout"] == expected
        raise RuntimeError("build intercepted")

    monkeypatch.setattr(preparation, "run", stop_at_build)
    with pytest.raises(RuntimeError, match="build intercepted"):
        preparation.prepare(profile, "0.3.0", "a" * 40, "123", tmp_path / "candidate")
