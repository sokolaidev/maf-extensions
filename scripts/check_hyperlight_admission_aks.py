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
        created_uids: dict[tuple[str, str], str] = {}
        try:
            for obj in objects:
                created = json.loads(
                    require(kubectl("create", "-f", "-", "-o", "json", payload=obj))
                )
                uid = created["metadata"].get("uid")
                if not isinstance(uid, str) or not uid:
                    raise RuntimeError("probe create response has no UID")
                created_uids[(obj["kind"], obj["metadata"]["name"])] = uid
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
            admission_names = {obj["kind"]: obj["metadata"]["name"] for obj in admission["items"]}
            for name in ("controller", "application"):
                identity = f"system:serviceaccount:{namespace}:{name}"
                checks = [
                    ("create", "pods", namespace, name == "controller"),
                    ("create", "pods", outside, False),
                    *[(verb, "secrets", namespace, False) for verb in ("get", "list", "watch")],
                    *[
                        (
                            verb,
                            f"{resource}/{admission_names[kind]}"
                            if verb in {"update", "patch", "delete"}
                            else resource,
                            namespace,
                            False,
                        )
                        for kind, resource in (
                            (
                                "ValidatingAdmissionPolicy",
                                "validatingadmissionpolicies.admissionregistration.k8s.io",
                            ),
                            (
                                "ValidatingAdmissionPolicyBinding",
                                "validatingadmissionpolicybindings.admissionregistration.k8s.io",
                            ),
                        )
                        for verb in ("create", "update", "patch", "delete", "deletecollection")
                    ],
                ]
                for verb, resource, ns, expected in checks:
                    result = kubectl("auth", "can-i", verb, resource, "-n", ns, identity=identity)
                    decision = result.stdout.strip().partition(" - ")[0]
                    allowed = decision == "yes"
                    if (
                        decision not in {"yes", "no"}
                        or result.returncode != (0 if allowed else 1)
                        or allowed != expected
                    ):
                        raise RuntimeError(
                            f"unexpected {name} authorization: {verb} {resource} "
                            f"in {'target' if ns == namespace else 'outside'}; "
                            f"expected {expected}, got {result.stdout.strip()!r} "
                            f"(exit {result.returncode}): {result.stderr}"
                        )
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
                    uid = created_uids.get((kind, name))
                    if uid is None:
                        raise RuntimeError(
                            "probe resource creation UID unavailable; manual cleanup required"
                        )
                    if current["metadata"].get("uid") != uid:
                        raise RuntimeError("probe resource UID changed")
                    api = (
                        "/api/v1"
                        if kind == "Namespace"
                        else "/apis/admissionregistration.k8s.io/v1"
                    )
                    collection = {
                        "Namespace": "namespaces",
                        "ValidatingAdmissionPolicy": "validatingadmissionpolicies",
                        "ValidatingAdmissionPolicyBinding": "validatingadmissionpolicybindings",
                    }[kind]
                    require(
                        kubectl(
                            "delete",
                            "--raw",
                            f"{api}/{collection}/{name}",
                            "-f",
                            "-",
                            payload={
                                "apiVersion": "v1",
                                "kind": "DeleteOptions",
                                "preconditions": {
                                    "uid": uid,
                                    "resourceVersion": current["metadata"]["resourceVersion"],
                                },
                            },
                        )
                    )
                    require(kubectl("wait", "--for=delete", kind, name, "--timeout=90s"))
                    if require(
                        kubectl("get", kind, name, "--ignore-not-found", "-o", "json")
                    ).strip():
                        raise RuntimeError("probe resource survived cleanup")
                except Exception as exc:
                    cleanup_errors.append(f"{kind}/{name}: {type(exc).__name__}: {exc}")
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
