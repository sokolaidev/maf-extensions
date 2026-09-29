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

FORBIDDEN_GRANTS = {
    f"grant-{identity}-{verb}-{resource}": (identity, verb, resource)
    for identity in ("controller", "application")
    for verb, resource in (
        ("list", "secrets"),
        ("watch", "secrets"),
        ("deletecollection", "validatingadmissionpolicies.admissionregistration.k8s.io"),
        ("deletecollection", "validatingadmissionpolicybindings.admissionregistration.k8s.io"),
    )
}
FORBIDDEN_GRANTS.update(
    {
        f"grant-named-{identity}-{verb}-{resource}": (
            identity,
            verb,
            resource + "/hyperlight-runtime-hyperlight-admission-test",
        )
        for identity in ("controller", "application")
        for verb in ("update", "patch", "delete")
        for resource in (
            "validatingadmissionpolicies.admissionregistration.k8s.io",
            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
        )
    }
)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "create-response",
        "create-missing-uid",
        "create-empty-uid",
        "replacement-namespace",
        "replacement-outside",
        "replacement-policy",
        "replacement-binding",
        "authorization",
        "authorization-response",
        "cleanup",
        "collision",
        "ownership-namespace",
        "ownership-outside",
        "ownership-policy",
        "ownership-binding",
        "ownership-missing-label",
        "race-label",
        "race-replacement",
        *FORBIDDEN_GRANTS,
    ],
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
    deleted: list[tuple[str, str]] = []
    foreign_key = {
        "ownership-namespace": ("Namespace", namespace),
        "ownership-outside": ("Namespace", namespace + "-outside"),
        "ownership-policy": ("ValidatingAdmissionPolicy", "hyperlight-runtime-" + namespace),
        "ownership-binding": (
            "ValidatingAdmissionPolicyBinding",
            "hyperlight-runtime-" + namespace,
        ),
        "ownership-missing-label": ("ValidatingAdmissionPolicy", "hyperlight-runtime-" + namespace),
    }.get(failure or "")
    replacement_key = {
        "replacement-namespace": ("Namespace", namespace),
        "replacement-outside": ("Namespace", namespace + "-outside"),
        "replacement-policy": ("ValidatingAdmissionPolicy", "hyperlight-runtime-" + namespace),
        "replacement-binding": (
            "ValidatingAdmissionPolicyBinding",
            "hyperlight-runtime-" + namespace,
        ),
    }.get(failure or "")
    uncertain_key = ("ValidatingAdmissionPolicy", "hyperlight-runtime-" + namespace)
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
                obj["metadata"].update(uid=f"uid-{len(created)}", resourceVersion="1")
                resources[key] = obj
                created.append(key)
                if failure == "create-response" and obj["kind"] == "ValidatingAdmissionPolicy":
                    raise subprocess.TimeoutExpired(command, 120)
            response = json.loads(json.dumps(obj))
            if obj["kind"] == "ValidatingAdmissionPolicy":
                if failure == "create-missing-uid":
                    del response["metadata"]["uid"]
                elif failure == "create-empty-uid":
                    response["metadata"]["uid"] = ""
            output = json.dumps(response)
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
            if identity and FORBIDDEN_GRANTS.get(failure or "") == (
                identity.rsplit(":", 1)[1],
                verb,
                resource,
            ):
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
            if "--raw" in args:
                url = args[args.index("--raw") + 1]
                collection, name = url.rsplit("/", 2)[-2:]
                kind = {
                    "namespaces": "Namespace",
                    "validatingadmissionpolicies": "ValidatingAdmissionPolicy",
                    "validatingadmissionpolicybindings": "ValidatingAdmissionPolicyBinding",
                }[collection]
                expected_api = (
                    "/api/v1" if kind == "Namespace" else "/apis/admissionregistration.k8s.io/v1"
                )
                assert url == f"{expected_api}/{collection}/{name}"
                assert args[-2:] == ["-f", "-"]
                options = json.loads(kwargs["input"])
                assert options["kind"] == "DeleteOptions"
                assert options["apiVersion"] == "v1"
                preconditions = options["preconditions"]
                key = (kind, name)
                assert preconditions == {
                    field: resources[key]["metadata"][field] for field in ("uid", "resourceVersion")
                }
            else:
                key = (args[1], args[2])
                preconditions = {}
            deleted.append(key)
            if failure == "cleanup":
                return subprocess.CompletedProcess(command, 1, "", "failed delete")
            if failure in {"race-label", "race-replacement"} and key == ("Namespace", namespace):
                metadata = resources[key]["metadata"]
                metadata["labels"]["hyperlight-admission-probe"] = "another-owner"
                if failure == "race-label":
                    metadata["resourceVersion"] = "2"
                else:
                    metadata["uid"] = "replacement-uid"
            if any(
                resources[key]["metadata"][field] != value for field, value in preconditions.items()
            ):
                return subprocess.CompletedProcess(command, 1, "", "409 Conflict")
            del resources[key]
        return subprocess.CompletedProcess(command, 0, output, "")

    def matrix(*args: Any) -> tuple[str, list[dict[str, str]]]:
        exercised.append(args)
        if failure is None:
            for resource in resources.values():
                resource["metadata"]["resourceVersion"] = "2"
        if replacement_key:
            resources[replacement_key]["metadata"]["uid"] = "replacement-uid"
        if foreign_key:
            labels = resources[foreign_key]["metadata"]["labels"]
            if failure == "ownership-missing-label":
                del labels["hyperlight-admission-probe"]
            else:
                labels["hyperlight-admission-probe"] = "another-owner"
        return "v1.35.7", [{"case": "shared-matrix", "result": "allowed"}]

    monkeypatch.setattr(aks, "prepare", prepare)
    monkeypatch.setattr(aks.subprocess, "run", run)
    monkeypatch.setattr(aks, "exercise_admission", matrix)
    output = tmp_path / "result.json"
    output.write_text("stale success", encoding="utf-8")
    if failure:
        with pytest.raises(
            (RuntimeError, ValueError, subprocess.TimeoutExpired),
            match="probe cleanup incomplete"
            if foreign_key or failure in {"race-label", "race-replacement"}
            else None,
        ) as error:
            aks.check(tmp_path / "policy.json", tmp_path / "config", "context", output)
        assert not output.exists()
        if failure in FORBIDDEN_GRANTS:
            identity, verb, resource = FORBIDDEN_GRANTS[failure]
            assert f"unexpected {identity} authorization: {verb} {resource}" in str(error.value)
            assert not exercised
        if failure == "cleanup":
            for kind, name in created:
                assert f"{kind}/{name}: RuntimeError: failed delete" in str(error.value)
        elif failure in {"race-label", "race-replacement"}:
            assert f"Namespace/{namespace}: RuntimeError: 409 Conflict" in str(error.value)
        elif replacement_key:
            assert "/".join(replacement_key) in str(error.value)
            assert "probe resource UID changed" in str(error.value)
        elif failure in {"create-response", "create-missing-uid", "create-empty-uid"}:
            assert "/".join(uncertain_key) in str(error.value)
            assert "probe resource creation UID unavailable" in str(error.value)
    else:
        aks.check(tmp_path / "policy.json", tmp_path / "config", "context", output)
        report = json.loads(output.read_text())
        assert report["probe_resources_removed"] is True
        assert len(report["permissions"]) == 30
        assert report["promotion"]["verifications"][0]["image"] == image
        assert len(exercised) == 1
    if failure == "collision":
        assert not created
        assert len(resources) == 1
    elif failure == "cleanup":
        assert len(resources) == 4
    elif failure in {"race-label", "race-replacement"}:
        assert set(resources) == {("Namespace", namespace)}
        assert (
            resources[("Namespace", namespace)]["metadata"]["labels"]["hyperlight-admission-probe"]
            == "another-owner"
        )
        assert ("Namespace", namespace) in deleted
    elif replacement_key:
        assert set(resources) == {replacement_key}
        assert replacement_key not in deleted
        assert set(deleted) == set(created) - {replacement_key}
    elif failure in {"create-response", "create-missing-uid", "create-empty-uid"}:
        assert set(resources) == {uncertain_key}
        assert uncertain_key not in deleted
        assert set(deleted) == set(created) - {uncertain_key}
    elif foreign_key:
        assert set(resources) == {foreign_key}
        assert foreign_key not in deleted
        assert set(deleted) == set(created) - {foreign_key}
    else:
        assert not resources


@pytest.mark.parametrize("input_name", ["policy.json", "config"])
def test_output_cannot_replace_inputs(tmp_path: Path, input_name: str) -> None:
    output = tmp_path / input_name
    output.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="output must differ"):
        aks.check(tmp_path / "policy.json", tmp_path / "config", "context", output)
    assert output.read_text() == "preserve"
