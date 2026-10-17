"""The Terraform release gate refuses drift, false successes and incomplete cleanup."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_container_release_image as runtime  # noqa: E402
import terraform_random_qualification as terraform  # noqa: E402

pytestmark = pytest.mark.workflow


def report():
    version = terraform.read(terraform.CONTEXT / "image.json")["engines"]["terraform"]["version"]
    return {
        "receipt": terraform.expected_receipt("dependencies.terraform.json"),
        "build": {
            "go_version": "go version "
            + terraform.read(terraform.CONTEXT / "image.json")["engines"]["terraform"][
                "source_build"
            ]["go_version"]
            + " linux/amd64",
            "source": terraform.read(terraform.CONTEXT / "image.json")["engines"]["terraform"][
                "source_build"
            ],
        },
        "providerBuild": terraform.read(terraform.CONTEXT / "dependencies.terraform.json")[
            "providers"
        ][0]["source_build"],
        "moduleFilesVerified": True,
        "checks": [
            {
                "name": name,
                "version": version,
                "error": None,
                "sourceUnchanged": True,
                "generatedLock": name == "pinned",
                "initExitCode": 1 if name not in terraform.SUCCESS else 0,
                "validateExitCode": None if name not in terraform.SUCCESS else 0,
                "validationValid": None if name not in terraform.SUCCESS else True,
                "providerLinks": 3,
                "providerCopies": 0,
                "providersInMirror": True,
                "moduleLoaded": True,
                "expectedRefusal": name not in terraform.SUCCESS,
            }
            for name in terraform.CASES
        ],
    }


def test_success_retains_catalogue_identity_and_consumer_results():
    result = terraform.evidence(json.dumps(report()).encode())
    assert [item["name"] for item in result["checks"]] == list(terraform.CASES)
    assert all(item["passed"] for item in result["checks"])
    compile(terraform.command()[-1], "terraform-random-probe", "exec")


@pytest.mark.parametrize("field", ["manifest_sha256", "policy_sha256", "engine", "schema"])
def test_receipt_drift_is_refused(field):
    data = report()
    data["receipt"][field] = "different"
    with pytest.raises(ValueError, match="receipt"):
        terraform.evidence(json.dumps(data).encode())


@pytest.mark.parametrize("kind", ["providers", "modules", "registry_modules"])
def test_artifact_inventory_drift_is_refused(kind):
    data = report()
    data["receipt"]["artifacts"][kind] = "0" * 64
    with pytest.raises(ValueError, match="receipt"):
        terraform.evidence(json.dumps(data).encode())


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "name", "wrong-order"),
        (0, "version", "0.0.0"),
        (0, "error", "launcher refused"),
        (0, "sourceUnchanged", False),
        (0, "generatedLock", False),
        (1, "generatedLock", True),
        (0, "initExitCode", 1),
        (0, "validateExitCode", 1),
        (0, "validationValid", False),
        (0, "providerLinks", 0),
        (0, "providerCopies", 1),
        (0, "providersInMirror", False),
        (0, "moduleLoaded", False),
        (1, "sourceUnchanged", False),
        (3, "initExitCode", 0),
        (3, "initExitCode", 124),
        (3, "validateExitCode", 0),
        (3, "validationValid", True),
        (3, "expectedRefusal", False),
        (4, "expectedRefusal", False),
        (4, "sourceUnchanged", False),
        (5, "initExitCode", 0),
        (5, "expectedRefusal", False),
        (6, "expectedRefusal", False),
        (6, "initExitCode", 0),
    ],
)
def test_false_success_is_refused(index, field, value):
    data = report()
    data["checks"][index][field] = value
    with pytest.raises(ValueError, match="consumer probe"):
        terraform.evidence(json.dumps(data).encode())


def test_missing_check_is_refused():
    data = report()
    data["checks"].pop()
    with pytest.raises(ValueError, match="incomplete"):
        terraform.evidence(json.dumps(data).encode())


@pytest.mark.parametrize("failure", [None, "receipt", "survivor"])
def test_runtime_checks_identity_and_observes_cleanup(monkeypatch, tmp_path, failure):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, "container" if command[1] == "ps" and failure == "survivor" else ""
        )

    async def output(command, *, timeout):
        assert timeout == 360
        data = report()
        if failure == "receipt":
            data["receipt"]["engine"] = "opentofu"
        return json.dumps(data).encode()

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime, "_smoke_output", output)
    if failure:
        with pytest.raises(ValueError):
            runtime.check("terraform-random", "sha256:" + "a" * 64, tmp_path)
    else:
        result = runtime.check("terraform-random", "sha256:" + "a" * 64, tmp_path)
        assert len(result["terraformRandom"]["checks"]) == 7
        assert result["containersRemaining"] == []
        assert result["liveDeploymentVerified"] is False
    name = commands[0][commands[0].index("--name") + 1]
    assert ["docker", "rm", "--force", name] in commands
    if failure != "receipt":
        assert commands[-1] == ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"]


def test_public_profile_uses_preparation_and_its_consumer_guide():
    from container_release import ROOT, profiles
    from select_image_security import PROFILES, affected

    script = (ROOT / "scripts/build_scan_image.sh").read_text()
    recipe = script.split("  terraform-random)", 1)[1].split("    ;;", 1)[0]
    assert "--engine terraform --profile builtin" in recipe
    assert "--build-arg MANIFEST=dependencies.terraform.json" in recipe
    assert "images/terraform-sandbox/prepared.Dockerfile" in recipe
    assert "images/terraform-sandbox/TERRAFORM.md" in recipe
    assert "terraform-prepared" not in profiles()
    assert "terraform-prepared" not in PROFILES
    assert "terraform-random" in affected("scripts/terraform_dependencies.py")
    assert affected("scripts/terraform_random_qualification.py") == ("terraform-random",)
    assert affected("images/terraform-sandbox/TERRAFORM.md") == ()


@pytest.mark.parametrize("field", ["build", "moduleFilesVerified", "providerBuild"])
def test_build_and_module_identity_drift_is_refused(field):
    data = report()
    data[field] = {"source": {}} if field == "build" else False
    with pytest.raises(ValueError, match="identity"):
        terraform.evidence(json.dumps(data).encode())


@pytest.mark.parametrize(
    "name,diagnostics,expected",
    [
        ("uncached-version", "no available releases match the given constraints 0.0.1", True),
        ("uncached-version", "Failed to retrieve available versions: timeout", False),
        ("wrong-lock", "Failed to install provider: doesn't match any of the\nchecksums", True),
        ("wrong-lock", "Failed to install provider: permission denied", False),
        ("missing-module", "Unreadable module directory", True),
        ("missing-module", "Invalid configuration", False),
        (
            "wrong-registry",
            "Failed to query available provider packages registry.opentofu.org/hashicorp/random",
            True,
        ),
        (
            "wrong-registry",
            "Failed to query available provider packages registry.terraform.io/hashicorp/random",
            False,
        ),
    ],
)
def test_refusals_require_the_expected_cause(name, diagnostics, expected):
    assert terraform.expected_refusal(name, diagnostics) is expected
