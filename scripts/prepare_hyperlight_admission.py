"""Freshly verify runtime candidates and prepare namespace-scoped admission policy."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from hyperlight_evidence import sha, sidecar_path
from verify_hyperlight_aks_image import verify_published_image

CANDIDATE_FIELDS = {
    "image",
    "signer_identity",
    "source_revision",
    "source_ref",
    "build_inputs_sha256",
}


def _admission(namespace: str, images: list[str]) -> dict[str, object]:
    name = f"hyperlight-runtime-{namespace}"
    approved = json.dumps(images)
    validations = [
        {
            "expression": f"object.spec.containers.all(c, c.image in {approved})",
            "message": "Hyperlight runtime image is not approved",
        },
        *[
            {
                "expression": (
                    f"!has(object.spec.{field}) || "
                    f"object.spec.{field}.all(c, c.image in {approved})"
                ),
                "message": "Hyperlight runtime image is not approved",
            }
            for field in ("initContainers", "ephemeralContainers")
        ],
        {
            "expression": (
                "!has(object.spec.volumes) || object.spec.volumes.all(v, !has(v.image))"
            ),
            "message": "OCI image volumes are not admitted",
        },
    ]
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingAdmissionPolicy",
                "metadata": {"name": name},
                "spec": {
                    "failurePolicy": "Fail",
                    "matchConstraints": {
                        "resourceRules": [
                            {
                                "apiGroups": [""],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE"],
                                "resources": ["pods", "pods/ephemeralcontainers"],
                                "scope": "Namespaced",
                            }
                        ],
                    },
                    "matchConditions": [
                        {
                            "name": "runtime-namespace",
                            "expression": f"request.namespace == {json.dumps(namespace)}",
                        }
                    ],
                    "validations": validations,
                },
            },
            {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingAdmissionPolicyBinding",
                "metadata": {"name": name},
                "spec": {"policyName": name, "validationActions": ["Deny"]},
            },
        ],
    }


def prepare(
    policy_path: Path,
    output: Path,
    *,
    trusted_root: Path | None = None,
) -> dict[str, object]:
    """Emit a bundle only after every candidate passes fresh provenance and payload checks."""
    sidecar = sidecar_path(output)
    if policy_path.resolve() in {output.resolve(), sidecar.resolve()} or (
        trusted_root is not None and trusted_root.resolve() in {output.resolve(), sidecar.resolve()}
    ):
        raise ValueError(
            "policy and trusted_root inputs must be different files from the output and evidence archive"
        )
    output.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or set(policy) != {"namespace", "candidates"}:
        raise ValueError("policy must contain only namespace and candidates")
    namespace = policy["namespace"]
    if (
        not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
        or namespace.startswith("kube-")
    ):
        raise ValueError("namespace must be a non-system DNS label")
    candidates = policy["candidates"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
        raise ValueError("policy requires one to eight candidates")
    images: list[str] = []
    for candidate in candidates:
        if (
            not isinstance(candidate, dict)
            or set(candidate) != CANDIDATE_FIELDS
            or any(not isinstance(v, str) or not v for v in candidate.values())
        ):
            raise ValueError("each candidate requires exactly the five verification fields")
        if candidate["image"] in images:
            raise ValueError("duplicate candidate image")
        images.append(candidate["image"])
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        evidence: list[dict[str, object]] = []
        archive_path = Path(temporary) / "evidence.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
            for index, candidate in enumerate(candidates):
                candidate_output = Path(temporary) / f"candidate-{index}.json"
                record = verify_published_image(
                    **candidate,
                    output=candidate_output,
                    trusted_root=trusted_root,
                )
                candidate_sidecar = sidecar_path(candidate_output)
                archive.write(candidate_sidecar, candidate_sidecar.name)
                evidence.append(record)
        bundle = {
            "schema_version": 1,
            "namespace": namespace,
            "admission": _admission(namespace, images),
            "verifications": evidence,
            "retained_evidence": {
                "archive": sidecar.name,
                "archive_sha256": sha(archive_path),
            },
        }
        staged = Path(temporary) / "promotion.json"
        staged.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.replace(archive_path, sidecar)
            os.replace(staged, output)
        except BaseException:
            output.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            raise
    return bundle


def main() -> None:
    """Run the operator-selected command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trusted-root", type=Path)
    args = parser.parse_args()
    prepare(args.policy, args.output, trusted_root=args.trusted_root)


if __name__ == "__main__":
    main()
