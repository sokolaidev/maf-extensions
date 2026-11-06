"""Published build identity stays fixed while current consumer probes run again."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import test_bicep_avm_qualification as bicep_avm_tests
import test_bicep_qualification as bicep_tests
import test_opentofu_random_qualification as tofu_tests
import test_terraform_avm_qualification as terraform_avm_tests
import test_terraform_random_qualification as terraform_tests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_container_release_image as runtime  # noqa: E402

IMAGE = "sha256:" + "a" * 64
PROFILES = [
    ("bicep", "bicep", bicep_tests.bicep, bicep_tests.report),
    ("bicep-avm", "bicepAvm", bicep_avm_tests.avm, bicep_avm_tests.report),
    ("terraform-random", "terraformRandom", terraform_tests.terraform, terraform_tests.report),
    ("opentofu-random", "opentofuRandom", tofu_tests.tofu, tofu_tests.report),
    ("terraform-avm", "terraformAvm", terraform_avm_tests.avm, terraform_avm_tests.report),
]


@pytest.mark.parametrize("profile,key,probe,report", PROFILES, ids=[p[0] for p in PROFILES])
@pytest.mark.parametrize("failure", [None, "identity", "build", "version", "runtime", "cleanup"])
def test_current_probes_use_published_identity_without_requiring_current_build_inputs(
    monkeypatch, tmp_path, profile, key, probe, report, failure
):
    data = report()
    if profile == "bicep-avm":
        data["receipt"]["files"] = {"module.json": "f" * 64}
    published = {
        "passed": True,
        "profile": profile,
        "imageId": IMAGE,
        "containersRemaining": [],
        key: probe.evidence(json.dumps(data).encode()),
    }
    monkeypatch.setattr(probe, "CONTEXT", tmp_path / "changed-image-sources")
    if failure == "identity":
        if profile == "bicep":
            data["configSha256"] = "f" * 64
        else:
            data["receipt"]["manifest_sha256"] = "f" * 64
    elif failure == "build":
        if profile.startswith("bicep"):
            data["buildReceipt"] = {}
        elif profile == "terraform-avm":
            data["receipt"]["policy_sha256"] = "f" * 64
        else:
            data["build"]["go_version"] = "different"
    elif failure == "version":
        if profile == "bicep":
            data["bicepVersion"] = "Bicep CLI version 0.0.0 (test)"
        elif profile == "bicep-avm":
            data["receipt"]["bicep_version"] = "Bicep CLI version 0.0.0 (test)"
        else:
            data["checks"][0]["version"] = "0.0.0"
    elif failure == "runtime":
        data["checks"].pop()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, "leftover" if command[1] == "ps" and failure == "cleanup" else ""
        )

    async def output(command, **kwargs):
        return json.dumps(data).encode()

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime, "_smoke_output", output)
    if failure:
        with pytest.raises(ValueError):
            runtime.check(profile, IMAGE, tmp_path, published_runtime=published)
    else:
        result = runtime.check(profile, IMAGE, tmp_path, published_runtime=published)
        assert result["passed"] is True
        assert result[key] == published[key]
        assert result["containersRemaining"] == []
    assert any(command[:3] == ["docker", "rm", "--force"] for command in commands)


@pytest.mark.parametrize(
    "field,value",
    [
        ("passed", False),
        ("profile", "other"),
        ("imageId", "sha256:" + "f" * 64),
        ("containersRemaining", ["leftover"]),
        ("bicep", {}),
    ],
)
def test_published_identity_must_belong_to_a_successfully_qualified_image(tmp_path, field, value):
    published = {
        "passed": True,
        "profile": "bicep",
        "imageId": IMAGE,
        "containersRemaining": [],
        "bicep": {"bicepVersion": "test"},
        field: value,
    }
    with pytest.raises(ValueError, match="Published runtime"):
        runtime.check("bicep", IMAGE, tmp_path, published_runtime=published)


def test_bicep_avm_command_targets_the_published_modules_and_version(monkeypatch, tmp_path):
    probe = bicep_avm_tests.avm
    published = probe.evidence(json.dumps(bicep_avm_tests.report()).encode())
    published["bicepVersion"] = "Bicep CLI version 0.1.0 (published)"
    for module in published["modules"]:
        module["reference"] = module["reference"].rsplit(":", 1)[0] + ":0.1.0"
    monkeypatch.setattr(probe, "CONTEXT", tmp_path / "changed-image-sources")
    command = probe.command(published=published)[-1]
    assert published["bicepVersion"] in command
    assert all(module["reference"] in command for module in published["modules"])
    assert "--no-restore" in command
