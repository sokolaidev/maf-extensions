"""Exercise a prepared admission bundle against a disposable Kubernetes API server."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

NODE_IMAGE = (
    "kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0"
)


def _command(args: list[str], payload: object | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=None if payload is None else json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=240,
    )


def _require(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode:
        raise RuntimeError(f"command failed: {result.stderr}")
    return result.stdout


def check(bundle_path: Path, output: Path) -> None:
    """Use an isolated KIND context; retain successful evidence only after cluster cleanup."""
    if bundle_path.resolve() == output.resolve():
        raise ValueError("bundle and output must be different files")
    output.unlink(missing_ok=True)
    raw = bundle_path.read_bytes()
    bundle = json.loads(raw)
    namespace = bundle["namespace"]
    approved = bundle["verifications"][0]["image"]
    refused = "unapproved.example/runtime@sha256:" + "0" * 64
    name = "hyperlight-admission-" + uuid.uuid4().hex[:8]
    clusters = _require(_command(["kind", "get", "clusters"])).splitlines()
    if name in clusters:
        raise RuntimeError("refusing to reuse an existing cluster")
    prefix = [
        "docker",
        "exec",
        "-i",
        f"{name}-control-plane",
        "kubectl",
        "--kubeconfig=/etc/kubernetes/admin.conf",
    ]
    cases: list[dict[str, str]] = []

    def kubectl(*args: str, payload: object | None = None) -> subprocess.CompletedProcess[str]:
        return _command([*prefix, *args], payload)

    def expect(label: str, result: subprocess.CompletedProcess[str], deny: bool = False) -> None:
        if deny:
            if result.returncode == 0 or (
                f"hyperlight-runtime-{namespace}" not in result.stderr
                or not any(
                    message in result.stderr
                    for message in (
                        "Hyperlight runtime image is not approved",
                        "OCI image volumes are not admitted",
                    )
                )
            ):
                raise RuntimeError(f"{label}: expected admission denial: {result.stderr}")
        else:
            _require(result)
        cases.append({"case": label, "result": "denied" if deny else "allowed"})

    container = {
        "name": "runtime",
        "image": approved,
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }
    pod: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "runtime", "namespace": namespace},
        "spec": {
            "containers": [container],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "automountServiceAccountToken": False,
            # Admission probes must never pull images or execute a runtime.
            "schedulingGates": [{"name": "provenance.test/hold"}],
        },
    }

    def create(label: str, value: dict[str, Any], deny: bool = False) -> None:
        expect(label, kubectl("create", "--dry-run=server", "-f", "-", payload=value), deny)

    with tempfile.TemporaryDirectory() as temporary:
        try:
            _require(
                _command(
                    [
                        "kind",
                        "create",
                        "cluster",
                        "--name",
                        name,
                        "--image",
                        NODE_IMAGE,
                        "--kubeconfig",
                        str(Path(temporary) / "config"),
                        "--wait",
                        "120s",
                    ]
                )
            )
            version = json.loads(_require(kubectl("version", "-o", "json")))["serverVersion"][
                "gitVersion"
            ]
            _require(kubectl("create", "namespace", namespace))
            _require(kubectl("create", "namespace", "outside-runtime"))
            _require(kubectl("apply", "-f", "-", payload=bundle["admission"]))
            deadline = time.monotonic() + 60
            while True:
                policy = json.loads(
                    _require(
                        kubectl(
                            "get",
                            "validatingadmissionpolicy",
                            f"hyperlight-runtime-{namespace}",
                            "-o",
                            "json",
                        )
                    )
                )
                status = policy.get("status", {})
                if status.get("observedGeneration") == policy["metadata"]["generation"]:
                    if status.get("typeChecking", {}).get("expressionWarnings"):
                        raise RuntimeError(f"admission type-check warnings: {status}")
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("admission policy status did not become ready")
                time.sleep(1)
            wrong = copy.deepcopy(pod)
            wrong["spec"]["containers"][0]["image"] = refused
            # The admission evaluator observes bindings asynchronously.
            while True:
                result = kubectl("create", "--dry-run=server", "-f", "-", payload=wrong)
                if result.returncode:
                    expect("binding-ready", result, True)
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("admission binding did not enforce denials")
                time.sleep(1)
            create("approved-runtime", pod)
            for label, image in (
                ("tag", approved.split("@", 1)[0] + ":latest"),
                ("wrong-digest", approved.split("@", 1)[0] + "@sha256:" + "0" * 64),
                ("wrong-registry", "unapproved.example/runtime@" + approved.split("@", 1)[1]),
            ):
                value = copy.deepcopy(pod)
                value["spec"]["containers"][0]["image"] = image
                create(label, value, True)
            value = copy.deepcopy(pod)
            value["spec"]["containers"].append({**container, "name": "other", "image": refused})
            create("additional-container", value, True)
            for sidecar in (False, True):
                value = copy.deepcopy(pod)
                init = {**container, "name": "init"}
                if sidecar:
                    init["restartPolicy"] = "Always"
                value["spec"]["initContainers"] = [init]
                suffix = "sidecar" if sidecar else "init"
                create("approved-" + suffix, value)
                init["image"] = refused
                create("unapproved-" + suffix, value, True)
            value = copy.deepcopy(wrong)
            value["metadata"]["labels"] = {"admission": "disabled"}
            create("labels-cannot-opt-out", value, True)
            value = copy.deepcopy(pod)
            value["spec"]["volumes"] = [{"name": "oci", "image": {"reference": refused}}]
            create("image-volume", value, True)
            value = copy.deepcopy(wrong)
            value["metadata"]["namespace"] = "outside-runtime"
            create("outside-namespace", value)
            _require(kubectl("create", "-f", "-", payload=pod))
            for label, image, deny in (
                ("approved-update", approved, False),
                ("unapproved-update", refused, True),
            ):
                patch = json.dumps({"spec": {"containers": [{**container, "image": image}]}})
                expect(
                    label,
                    kubectl(
                        "patch",
                        "pod",
                        "runtime",
                        "-n",
                        namespace,
                        "--type=merge",
                        "--patch",
                        patch,
                        "--dry-run=server",
                    ),
                    deny,
                )
            for label, image, deny in (
                ("approved-ephemeral", approved, False),
                ("unapproved-ephemeral", refused, True),
            ):
                value = json.loads(
                    _require(kubectl("get", "pod", "runtime", "-n", namespace, "-o", "json"))
                )
                value["spec"]["ephemeralContainers"] = [
                    {
                        **container,
                        "name": "debug",
                        "image": image,
                    }
                ]
                expect(
                    label,
                    kubectl(
                        "replace",
                        "--raw",
                        f"/api/v1/namespaces/{namespace}/pods/runtime/ephemeralcontainers?dryRun=All",
                        "-f",
                        "-",
                        payload=value,
                    ),
                    deny,
                )
        finally:
            _require(_command(["kind", "delete", "cluster", "--name", name]))
            if name in _require(_command(["kind", "get", "clusters"])).splitlines():
                raise RuntimeError("test cluster survived cleanup")
    record = {
        "schema_version": 1,
        "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "kubernetes_version": version,
        "node_image": NODE_IMAGE,
        "cases": cases,
        "cluster_removed": True,
        "runtime_execution_verified": False,
        "production_admission_verified": False,
    }
    output.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    """Run the operator-selected command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.bundle, args.output)


if __name__ == "__main__":
    main()
