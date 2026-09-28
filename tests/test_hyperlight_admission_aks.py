"""The AKS probe must isolate ownership and clear stale success on failure."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_hyperlight_admission_aks as aks
import prepare_hyperlight_admission as admission

pytestmark = pytest.mark.workflow


@pytest.mark.parametrize(
    "failure",
    [None, "create-response", "authorization", "authorization-response", "cleanup", "collision"],
)
@pytest.mark.parametrize("denial", ["no", "no - Azure does not have opinion for this user."])
def test_probe_ownership_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None, denial: str
) -> None:
    namespace = "hyperlight-admission-test"
    image = "registry.example/runtime@sha256:" + "a" * 64
    bundle = {
        "namespace": namespace,
        "admission": admission._admission(namespace, [image]),
        "verifications": [
            {
                "image": image,
                "registry_digest": "sha256:" + "a" * 64,
                "policy": {
                    "source_revision": "b" * 40,
                    "signer_identity": "signer",
                    "build_inputs_sha256": "c" * 64,
                },
            }
        ],
    }
    resources: dict[tuple[str, str], Any] = {}
    created: list[tuple[str, str]] = []
    exercised = []
    if failure == "collision":
        resources[("Namespace", namespace)] = {"metadata": {"labels": {}}}

    def prepare(*args: Any) -> dict[str, Any]:
        return bundle

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = command[6:]
        identity = None
        if "--as" in args:
            identity = args[args.index("--as") + 1]
            args = args[8:]
        action = args[0]
        output = ""
        if action == "get":
            obj = resources.get((args[1], args[2]))
            output = json.dumps(obj) if obj else ""
        elif action == "create" and "-f" in args:
            obj = json.loads(kwargs["input"])
            if obj["kind"] in {
                "Namespace",
                "ValidatingAdmissionPolicy",
                "ValidatingAdmissionPolicyBinding",
            }:
                key = (obj["kind"], obj["metadata"]["name"])
                resources[key] = obj
                created.append(key)
                if failure == "create-response" and obj["kind"] == "ValidatingAdmissionPolicy":
                    raise subprocess.TimeoutExpired(command, 120)
            output = json.dumps(obj)
        elif action == "auth":
            verb, resource = args[2:4]
            target = args[args.index("-n") + 1]
            allowed = (
                identity is not None
                and identity.endswith(":controller")
                and verb == "create"
                and resource == "pods"
                and target == namespace
            )
            if failure == "authorization" and resource.startswith("validatingadmissionpolicies"):
                allowed = True
            return subprocess.CompletedProcess(
                command,
                0 if allowed else 1,
                "no unexpected"
                if failure == "authorization-response"
                else "yes"
                if allowed
                else denial,
                "",
            )
        elif action == "delete":
            if failure == "cleanup":
                return subprocess.CompletedProcess(command, 1, "", "failed delete")
            del resources[(args[1], args[2])]
        return subprocess.CompletedProcess(command, 0, output, "")

    def matrix(*args: Any) -> tuple[str, list[dict[str, str]]]:
        exercised.append(args)
        return "v1.35.7", [{"case": "shared-matrix", "result": "allowed"}]

    monkeypatch.setattr(aks, "prepare", prepare)
    monkeypatch.setattr(aks.subprocess, "run", run)
    monkeypatch.setattr(aks, "exercise_admission", matrix)
    output = tmp_path / "result.json"
    output.write_text("stale success", encoding="utf-8")
    if failure:
        with pytest.raises((RuntimeError, ValueError, subprocess.TimeoutExpired)):
            aks.check(tmp_path / "policy.json", tmp_path / "config", "context", output)
        assert not output.exists()
    else:
        aks.check(tmp_path / "policy.json", tmp_path / "config", "context", output)
        report = json.loads(output.read_text())
        assert report["probe_resources_removed"] is True
        assert len(report["permissions"]) == 22
        assert report["promotion"]["verifications"][0]["image"] == image
        assert len(exercised) == 1
    if failure == "collision":
        assert not created
        assert len(resources) == 1
    elif failure == "cleanup":
        assert len(resources) == 4
    else:
        assert not resources


@pytest.mark.parametrize("input_name", ["policy.json", "config"])
def test_output_cannot_replace_inputs(tmp_path: Path, input_name: str) -> None:
    output = tmp_path / input_name
    output.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="output must differ"):
        aks.check(tmp_path / "policy.json", tmp_path / "config", "context", output)
    assert output.read_text() == "preserve"
