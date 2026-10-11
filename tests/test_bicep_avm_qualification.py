"""Release qualification must prove usable cached AVM modules and expected refusals."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bicep_avm_qualification as avm  # noqa: E402
import check_container_release_image as runtime  # noqa: E402


def report():
    manifest = avm.read(avm.CONTEXT / "dependencies.bicep-avm.json")
    return {
        "receipt": {
            "schema": 1,
            "manifest_sha256": hashlib.sha256(
                (avm.CONTEXT / "dependencies.bicep-avm.json").read_bytes()
            ).hexdigest(),
            "policy_sha256": hashlib.sha256(
                (avm.CONTEXT / "dependencies.bicep-avm.policy.json").read_bytes()
            ).hexdigest(),
            "bicep_version": f"Bicep CLI version {manifest['bicep_version']} (test)",
            "modules": manifest["modules"],
        },
        "checks": [
            {
                "name": case["name"],
                "exitCode": 1 if case["errors"] else 0,
                "outputPresent": not case["errors"],
                "diagnostics": {
                    "runs": [
                        {"results": [{"ruleId": code, "level": "error"} for code in case["errors"]]}
                    ]
                },
            }
            for case in avm.cases()
        ],
    }


def test_success_records_both_modules_and_refusals():
    result = avm.evidence(json.dumps(report()).encode())
    assert [item["name"] for item in result["checks"]] == [
        "virtual-network",
        "storage-account",
        "invalid-parameter",
        "uncached-version",
        "missing-cache-config",
    ]
    assert all(item["passed"] for item in result["checks"])


@pytest.mark.parametrize(
    "change",
    [
        "manifest",
        "policy",
        "module",
        "version",
        "missing-check",
        "empty-diagnostics",
        "unexpected-error",
        "unexpected-success",
        "unexpected-output",
        "missing-output",
        "wrong-order",
    ],
)
def test_false_success_is_refused(change):
    data = report()
    if change in {"manifest", "policy"}:
        data["receipt"][change + "_sha256"] = "0" * 64
    elif change == "module":
        data["receipt"]["modules"].pop()
    elif change == "version":
        data["receipt"]["bicep_version"] = "Bicep CLI version 0.0.0 (test)"
    elif change == "missing-check":
        data["checks"].pop()
    elif change == "empty-diagnostics":
        data["checks"][0]["diagnostics"]["runs"] = []
    elif change == "unexpected-error":
        data["checks"][0]["diagnostics"]["runs"][0]["results"] = [
            {"ruleId": "BCP190", "level": "error"}
        ]
    elif change == "unexpected-success":
        data["checks"][-1]["exitCode"] = 0
    elif change == "unexpected-output":
        data["checks"][-1]["outputPresent"] = True
    elif change == "missing-output":
        data["checks"][0]["outputPresent"] = False
    elif change == "wrong-order":
        data["checks"].reverse()
    with pytest.raises(ValueError):
        avm.evidence(json.dumps(data).encode())


def test_every_compilation_disables_restore():
    script = avm.command()[-1]
    builds = [line for line in script.splitlines() if line.startswith("bicep build ")]
    assert len(builds) == 5
    assert all(" --no-restore " in line for line in builds)
    assert "bicep restore" not in script


@pytest.mark.parametrize("failure", [None, "diagnostics", "survivor"])
def test_runtime_checks_evidence_and_observes_cleanup(monkeypatch, tmp_path, failure):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, "container" if command[1] == "ps" and failure == "survivor" else ""
        )

    async def output(command):
        data = report()
        if failure == "diagnostics":
            data["checks"].pop()
        return json.dumps(data).encode()

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime, "_smoke_output", output)
    if failure:
        with pytest.raises(ValueError):
            runtime.check("bicep-avm", "sha256:" + "a" * 64, tmp_path)
    else:
        result = runtime.check("bicep-avm", "sha256:" + "a" * 64, tmp_path)
        assert len(result["bicepAvm"]["checks"]) == 5
        assert result["containersRemaining"] == []
    name = commands[0][commands[0].index("--name") + 1]
    assert ["docker", "rm", "--force", name] in commands
    if failure != "diagnostics":
        assert commands[-1] == ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"]


def test_old_public_profile_is_not_an_alias(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime.subprocess, "run", lambda *a, **k: pytest.fail("must refuse before Docker")
    )
    with pytest.raises(ValueError, match="Unknown image profile"):
        runtime.check("bicep-prepared", "sha256:" + "a" * 64, tmp_path)
