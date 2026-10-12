"""Base Bicep publication requires compiler, policy and refusal evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bicep_qualification as bicep  # noqa: E402
import check_container_release_image as runtime  # noqa: E402
from bicep_image_pin import read_pin  # noqa: E402
from container_release_approval import UNAVAILABLE, evidence_rows  # noqa: E402


def report():
    version, _ = read_pin(bicep.CONTEXT / "Dockerfile")
    template = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "resources": [],
        "parameters": {"name": {"type": "string", "defaultValue": "release-probe"}},
        "outputs": {"result": {"type": "string", "value": "[parameters('name')]"}},
    }
    return {
        "bicepVersion": f"Bicep CLI version {version.removeprefix('v')} (test)",
        **bicep.identities(),
        "checks": [
            {
                "name": case["name"],
                "exitCode": 1 if case["errors"] else 0,
                "template": None if case["errors"] else template.copy(),
                "diagnostics": {
                    "runs": [
                        {
                            "results": [
                                {"ruleId": code, "level": level}
                                for key, level in (("errors", "error"), ("warnings", "warning"))
                                for code in case[key]
                            ]
                        }
                    ]
                },
            }
            for case in bicep.cases()
        ],
    }


def test_records_compiler_policy_refusals_and_execution_scope():
    result = bicep.evidence(json.dumps(report()).encode())
    assert result["backend"] == "docker-cli"
    assert result["network"] == "none"
    assert [check["name"] for check in result["checks"]] == [
        "valid-local",
        "invalid-type",
        "fallback-config",
        "default-config-control",
        "staged-config",
        "uncached-module",
    ]
    assert all(check["passed"] for check in result["checks"])


@pytest.mark.parametrize(
    "change",
    [
        "bicepVersion",
        "compilerSha256",
        "configSha256",
        "licenseSha256",
        "missing-check",
        "reordered",
        "empty-diagnostics",
        "unexpected-warning",
        "policy-not-applied",
        "wrong-refusal",
        "wrong-exit",
        "missing-template",
        "invalid-template",
        "empty-template",
        "wrong-output",
        "output-on-failure",
    ],
)
def test_false_success_is_refused(change):
    data = report()
    if change in data:
        data[change] = "wrong"
    elif change == "missing-check":
        data["checks"].pop()
    elif change == "reordered":
        data["checks"].reverse()
    elif change == "empty-diagnostics":
        data["checks"][0]["diagnostics"]["runs"] = []
    elif change == "unexpected-warning":
        data["checks"][0]["diagnostics"]["runs"][0]["results"] = [
            {"ruleId": "unknown", "level": "warning"}
        ]
    elif change == "policy-not-applied":
        data["checks"][2]["diagnostics"]["runs"][0]["results"][0]["level"] = "warning"
    elif change == "wrong-refusal":
        data["checks"][-1]["diagnostics"]["runs"][0]["results"][0]["ruleId"] = "BCP036"
    elif change == "wrong-exit":
        data["checks"][1]["exitCode"] = 0
    elif change == "missing-template":
        data["checks"][0]["template"] = None
    elif change == "invalid-template":
        data["checks"][0]["template"] = "not a template"
    elif change == "empty-template":
        data["checks"][0]["template"] = {}
    elif change == "wrong-output":
        data["checks"][0]["template"]["outputs"] = {}
    elif change == "output-on-failure":
        data["checks"][1]["template"] = {}
    with pytest.raises(ValueError):
        bicep.evidence(json.dumps(data).encode())


@pytest.mark.parametrize("failure", [None, "evidence", "timeout", "survivor"])
def test_release_gate_bounds_execution_and_removes_container(monkeypatch, tmp_path, failure):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, "container" if command[1] == "ps" and failure == "survivor" else ""
        )

    async def output(command):
        if failure == "timeout":
            raise TimeoutError("compiler")
        data = report()
        if failure == "evidence":
            data["checks"].pop()
        return json.dumps(data).encode()

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime, "_smoke_output", output)
    if failure:
        with pytest.raises((ValueError, TimeoutError)):
            runtime.check("bicep", "sha256:" + "a" * 64, tmp_path)
    else:
        result = runtime.check("bicep", "sha256:" + "a" * 64, tmp_path)
        assert len(result["bicep"]["checks"]) == 6
        assert result["containersRemaining"] == []
    create = commands[0]
    assert create[create.index("--network") + 1] == "none"
    assert "--read-only" in create
    assert f"{bicep.WORK}:rw,noexec,size=16m" in create
    assert create[-1] == bicep.command()[-1]
    name = create[create.index("--name") + 1]
    assert ["docker", "rm", "--force", name] in commands
    if failure not in {"evidence", "timeout"}:
        assert commands[-1] == ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"]


def test_approval_shows_evidence_and_does_not_invent_missing_checks():
    candidate = {"profile": "bicep", "imageId": "sha256:" + "a" * 64}
    retained = {
        "runtime.json": {**candidate, "bicep": bicep.evidence(json.dumps(report()).encode())}
    }
    rows = evidence_rows(retained, candidate)
    assert len([row for row in rows if row[0] == "Bicep check"]) == 6
    assert ("Bicep check network", "none") in rows
    assert ("Bicep check backend", "docker-cli") in rows
    missing = evidence_rows({}, candidate)
    assert ("Bicep check", UNAVAILABLE) in missing
    assert ("Bicep license SHA-256", UNAVAILABLE) in missing


@pytest.mark.parametrize("explicit", [False, True])
def test_sarif_warning_level_may_be_omitted(explicit):
    data = report()
    if not explicit:
        del data["checks"][3]["diagnostics"]["runs"][0]["results"][0]["level"]
    result = bicep.evidence(json.dumps(data).encode())
    assert result["checks"][3]["warning"] == ["no-unused-params"]
