"""Run the shared runtime admission probes in temporary namespaces on an explicit AKS context."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml
from check_hyperlight_admission_live import exercise_admission
from prepare_hyperlight_admission import prepare

ROOT = Path(__file__).resolve().parents[1]


def check(policy: Path, kubeconfig: Path, context: str, output: Path) -> None:
    """Freshly verify candidates, probe isolated resources, and require cleanup before success."""
    if output.resolve() in {policy.resolve(), kubeconfig.resolve()}:
        raise ValueError("output must differ from policy and kubeconfig")
    output.unlink(missing_ok=True)
    prefix = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        context,
        "--request-timeout=30s",
    ]

    def kubectl(
        *args: str, payload: object | None = None, identity: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = [*prefix]
        if identity:
            command.extend(
                [
                    "--as",
                    identity,
                    "--as-group",
                    "system:authenticated",
                    "--as-group",
                    "system:serviceaccounts",
                    "--as-group",
                    f"system:serviceaccounts:{namespace}",
                ]
            )
        return subprocess.run(
            [*command, *args],
            input=None if payload is None else json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def require(result: subprocess.CompletedProcess[str]) -> str:
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    owner = uuid.uuid4().hex
    permission_cases: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        bundle = prepare(policy, Path(temporary) / "promotion.json")
        namespace = str(bundle["namespace"])
        if not namespace.startswith("hyperlight-admission-") or len(namespace) > 50:
            raise ValueError(
                "probe namespace must start with hyperlight-admission- and be at most 50 characters"
            )
        outside = namespace + "-outside"
        admission: Any = bundle["admission"]
        namespace_objects = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": name,
                    "labels": {"pod-security.kubernetes.io/enforce": "restricted"},
                },
            }
            for name in (namespace, outside)
        ]
        objects = [*namespace_objects, *admission["items"]]
        for obj in objects:
            obj["metadata"].setdefault("labels", {})["hyperlight-admission-probe"] = owner
        # Refuse all collisions before taking ownership of any resource.
        for obj in objects:
            if require(
                kubectl(
                    "get", obj["kind"], obj["metadata"]["name"], "--ignore-not-found", "-o", "json"
                )
            ).strip():
                raise ValueError("probe resource already exists")
        try:
            for obj in objects:
                require(kubectl("create", "-f", "-", "-o", "json", payload=obj))
            role = yaml.safe_load(
                (ROOT / "images/hyperlight-sandbox/controller-role.yaml").read_text()
            )
            role["metadata"]["namespace"] = namespace
            require(kubectl("create", "-f", "-", payload=role))
            for name in ("controller", "application"):
                require(kubectl("create", "serviceaccount", name, "-n", namespace))
            require(
                kubectl(
                    "create",
                    "rolebinding",
                    "probe-controller",
                    "-n",
                    namespace,
                    "--role=hyperlight-pod-controller",
                    f"--serviceaccount={namespace}:controller",
                )
            )
            for name in ("controller", "application"):
                identity = f"system:serviceaccount:{namespace}:{name}"
                checks = [
                    ("create", "pods", namespace, name == "controller"),
                    ("create", "pods", outside, False),
                    ("get", "secrets", namespace, False),
                    *[
                        (verb, resource, namespace, False)
                        for resource in (
                            "validatingadmissionpolicies.admissionregistration.k8s.io",
                            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
                        )
                        for verb in ("create", "update", "patch", "delete")
                    ],
                ]
                for verb, resource, ns, expected in checks:
                    result = kubectl("auth", "can-i", verb, resource, "-n", ns, identity=identity)
                    allowed = result.stdout.strip() == "yes"
                    if (
                        result.stdout.strip() not in {"yes", "no"}
                        or result.returncode not in {0, 1}
                        or allowed != expected
                    ):
                        raise RuntimeError("unexpected application/controller authorization")
                    permission_cases.append(
                        {
                            "identity": name,
                            "verb": verb,
                            "resource": resource,
                            "scope": "target" if ns == namespace else "outside",
                            "allowed": allowed,
                        }
                    )
            verifications: Any = bundle["verifications"]
            version, cases = exercise_admission(
                kubectl, namespace, verifications[0]["image"], outside
            )
        finally:
            cleanup_errors: list[str] = []
            for obj in reversed(objects):
                kind, name = obj["kind"], obj["metadata"]["name"]
                try:
                    raw = require(kubectl("get", kind, name, "--ignore-not-found", "-o", "json"))
                    if not raw.strip():
                        continue
                    current = json.loads(raw)
                    if (
                        current["metadata"].get("labels", {}).get("hyperlight-admission-probe")
                        != owner
                    ):
                        raise RuntimeError("probe resource ownership changed")
                    require(kubectl("delete", kind, name, "--wait=true", "--timeout=90s"))
                    if require(
                        kubectl("get", kind, name, "--ignore-not-found", "-o", "json")
                    ).strip():
                        raise RuntimeError("probe resource survived cleanup")
                except Exception as exc:
                    cleanup_errors.append(f"{kind}: {type(exc).__name__}")
            if cleanup_errors:
                raise RuntimeError("probe cleanup incomplete: " + ", ".join(cleanup_errors))
        record = {
            "schema_version": 1,
            "kubernetes_version": version,
            "image_digest": verifications[0]["registry_digest"],
            "source_revision": verifications[0]["policy"]["source_revision"],
            "signer_identity": verifications[0]["policy"]["signer_identity"],
            "build_inputs_sha256": verifications[0]["policy"]["build_inputs_sha256"],
            "admission_sha256": hashlib.sha256(
                json.dumps(admission, sort_keys=True).encode()
            ).hexdigest(),
            "cases": cases,
            "permissions": permission_cases,
            "probe_resources_removed": True,
            "runtime_execution_verified": False,
        }
        record["promotion"] = bundle
        output.write_text(json.dumps(record, indent=2), encoding="utf-8")


def main() -> None:
    """Run isolated API probes using explicit operator policy and cluster context."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.policy, args.kubeconfig, args.context, args.output)


if __name__ == "__main__":
    main()
