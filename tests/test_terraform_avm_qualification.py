"""The AVM release gate refuses drift, false successes and incomplete cleanup."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_container_release_image as runtime  # noqa: E402
import terraform_avm_qualification as avm  # noqa: E402

pytestmark = pytest.mark.workflow


def report():
    version = avm.read(avm.CONTEXT / "image.json")["engines"]["terraform"]["version"]
    return {
        "receipt": avm.expected_receipt(),
        "checks": [
            {
                "name": name,
                "version": version,
                "error": None,
                "sourceUnchanged": True,
                "initExitCode": 1 if name in {"uncached-version", "wrong-lock"} else 0,
                "validateExitCode": None if name in {"uncached-version", "wrong-lock"} else 0,
                "validationValid": None if name in {"uncached-version", "wrong-lock"} else True,
                "providerLinks": 3,
                "providerCopies": 0,
                "providersInMirror": True,
                "nestedModuleLoaded": True,
                "expectedRefusal": name in {"uncached-version", "wrong-lock"},
            }
            for name in avm.CASES
        ],
    }


def test_success_retains_catalogue_identity_and_consumer_results():
    result = avm.evidence(json.dumps(report()).encode())
    assert [item["name"] for item in result["checks"]] == list(avm.CASES)
    assert all(item["passed"] for item in result["checks"])
    compile(avm.command()[-1], "terraform-avm-probe", "exec")


@pytest.mark.parametrize("field", ["manifest_sha256", "policy_sha256", "engine", "schema"])
def test_receipt_drift_is_refused(field):
    data = report()
    data["receipt"][field] = "different"
    with pytest.raises(ValueError, match="receipt"):
        avm.evidence(json.dumps(data).encode())


@pytest.mark.parametrize("kind", ["providers", "modules", "registry_modules"])
def test_artifact_inventory_drift_is_refused(kind):
    data = report()
    data["receipt"]["artifacts"][kind] = "0" * 64
    with pytest.raises(ValueError, match="receipt"):
        avm.evidence(json.dumps(data).encode())


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "name", "wrong-order"),
        (0, "version", "0.0.0"),
        (0, "error", "launcher refused"),
        (0, "sourceUnchanged", False),
        (0, "initExitCode", 1),
        (0, "validateExitCode", 1),
        (0, "validationValid", False),
        (0, "providerLinks", 0),
        (0, "providerCopies", 1),
        (0, "providersInMirror", False),
        (0, "nestedModuleLoaded", False),
        (2, "sourceUnchanged", False),
        (3, "initExitCode", 0),
        (3, "initExitCode", 124),
        (3, "validateExitCode", 0),
        (3, "validationValid", True),
        (3, "expectedRefusal", False),
        (4, "expectedRefusal", False),
        (4, "sourceUnchanged", False),
    ],
)
def test_false_success_is_refused(index, field, value):
    data = report()
    data["checks"][index][field] = value
    with pytest.raises(ValueError, match="consumer probe"):
        avm.evidence(json.dumps(data).encode())


def test_missing_check_is_refused():
    data = report()
    data["checks"].pop()
    with pytest.raises(ValueError, match="incomplete"):
        avm.evidence(json.dumps(data).encode())


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
            runtime.check("terraform-avm", "sha256:" + "a" * 64, tmp_path)
    else:
        result = runtime.check("terraform-avm", "sha256:" + "a" * 64, tmp_path)
        assert len(result["terraformAvm"]["checks"]) == 5
        assert result["containersRemaining"] == []
        assert result["liveDeploymentVerified"] is False
    name = commands[0][commands[0].index("--name") + 1]
    assert ["docker", "rm", "--force", name] in commands
    if failure != "receipt":
        assert commands[-1] == ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"]


def test_build_uses_full_catalogue_not_network_fixture():
    script = (avm.ROOT / "scripts/build_scan_image.sh").read_text()
    recipe = script.split("  terraform-avm)", 1)[1].split("    ;;", 1)[0]
    assert "--engine terraform --profile builtin" in recipe
    assert "--build-arg MANIFEST=dependencies.terraform-avm.json" in recipe
    assert "dependencies.terraform-avm-network.json" not in recipe
    assert "images/terraform-sandbox/prepared.Dockerfile" in recipe
    assert "images/terraform-sandbox/AVM.md" in recipe


@pytest.mark.parametrize(
    "name,diagnostics,expected",
    [
        (
            "uncached-version",
            "host registry.terraform.io does not provide a modules\nservice.",
            True,
        ),
        ("uncached-version", "Failed to retrieve available versions: timeout", False),
        ("wrong-lock", "Failed to install provider: doesn't match any of the\nchecksums", True),
        ("wrong-lock", "Failed to install provider: permission denied", False),
    ],
)
def test_refusals_tolerate_line_wrapping_but_require_the_expected_reason(
    name, diagnostics, expected
):
    assert avm.expected_refusal(name, diagnostics) is expected
