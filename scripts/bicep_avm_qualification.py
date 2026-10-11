"""Offline release probes for the selected Bicep AVM cache."""

from __future__ import annotations

import hashlib
import json
import shlex
from typing import Any

from container_release import ROOT, read

CONTEXT = ROOT / "images/bicep-sandbox"


def cases() -> list[dict[str, Any]]:
    """Cover each approved module plus schema and missing-cache refusals."""
    references = read(CONTEXT / "dependencies.bicep-avm.policy.json")["modules"]
    parameters = {
        "br/public:avm/res/network/virtual-network": "name: 'release-network'\n    addressPrefixes: ['10.0.0.0/16']",
        "br/public:avm/res/storage/storage-account": "name: 'releasestorage'",
    }
    result = []
    for reference in references:
        module = reference.rsplit(":", 1)[0]
        if module not in parameters:
            raise ValueError("Add a consumer probe before extending the AVM release profile")
        source = f"module resource '{reference}' = {{\n  name: 'probe'\n  params: {{\n    {parameters[module]}\n  }}\n}}\n"
        result.append(
            {"name": module.rsplit("/", 1)[-1], "source": source, "errors": [], "config": True}
        )
    network = next(case for case in result if case["name"] == "virtual-network")
    reference = next(ref for ref in references if "/virtual-network:" in ref)
    result.extend(
        [
            network
            | {
                "name": "invalid-parameter",
                "source": network["source"].replace("'release-network'", "42"),
                "errors": ["BCP036"],
            },
            network
            | {
                "name": "uncached-version",
                "source": network["source"].replace(
                    reference, reference.rsplit(":", 1)[0] + ":0.0.0"
                ),
                "errors": ["BCP190"],
            },
            network | {"name": "missing-cache-config", "config": False, "errors": ["BCP190"]},
        ]
    )
    return result


def command() -> list[str]:
    """Compile bounded fixtures using only the image's existing module cache."""
    version = read(CONTEXT / "dependencies.bicep-avm.policy.json")["bicep_version"]
    lines = [
        "set -eu",
        f"bicep --version | grep -F {shlex.quote('Bicep CLI version ' + version + ' ')} >&2",
        "printf '{\"receipt\":'",
        "cat /opt/maf-bicep/dependencies.json",
        "printf ',\"checks\":['",
    ]
    for index, case in enumerate(cases()):
        name = case["name"]
        lines.extend([f"mkdir /tmp/{name}", f"cd /tmp/{name}", "export HOME=$PWD"])
        if case["config"]:
            lines.append("cp /maf-sandbox/work/bicepconfig.json bicepconfig.json")
        lines.extend(
            [
                f"printf %s {shlex.quote(case['source'])} > main.bicep",
                "status=0",
                "bicep build main.bicep --no-restore --diagnostics-format sarif --outfile main.json > stdout.txt 2> diagnostics.json || status=$?",
                f'printf \'{"," if index else ""}{{"name":"{name}","exitCode":%s,"diagnostics":\' "$status"',
                "cat diagnostics.json",
                "printf ',\"outputPresent\":'",
                "if test -s main.json; then printf true; else printf false; fi",
                "printf '}'",
            ]
        )
    lines.append("printf ']}'")
    return ["sh", "-ec", "\n".join(lines)]


def evidence(output: bytes) -> dict[str, Any]:
    """Refuse missing modules, wrong diagnostics and receipts from another policy."""
    result = json.loads(output)
    receipt = result["receipt"]
    manifest_path = CONTEXT / "dependencies.bicep-avm.json"
    policy_path = CONTEXT / "dependencies.bicep-avm.policy.json"
    manifest = read(manifest_path)
    if (
        receipt.get("schema") != 1
        or receipt.get("manifest_sha256") != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        or receipt.get("policy_sha256") != hashlib.sha256(policy_path.read_bytes()).hexdigest()
        or receipt.get("modules") != manifest["modules"]
        or not receipt.get("bicep_version", "").startswith(
            f"Bicep CLI version {manifest['bicep_version']} "
        )
    ):
        raise ValueError("Bicep AVM receipt differs from the selected release policy")
    expected = cases()
    observed = result["checks"]
    if not isinstance(observed, list) or len(observed) != len(expected):
        raise ValueError("Bicep AVM qualification cases are incomplete")
    checks = []
    for actual, case in zip(observed, expected, strict=True):
        runs = actual["diagnostics"]["runs"]
        errors = sorted(
            {
                finding["ruleId"]
                for run in runs
                for finding in run["results"]
                if finding["level"] == "error"
            }
        )
        if (
            actual["name"] != case["name"]
            or not runs
            or errors != case["errors"]
            or actual["exitCode"] != (1 if errors else 0)
            or actual["outputPresent"] is not (not errors)
        ):
            raise ValueError(f"Bicep AVM consumer probe failed: {case['name']}")
        checks.append(
            {
                "name": case["name"],
                "passed": True,
                "errors": errors,
                "outputPresent": actual["outputPresent"],
            }
        )
    return {
        "bicepVersion": receipt["bicep_version"],
        "manifestSha256": receipt["manifest_sha256"],
        "policySha256": receipt["policy_sha256"],
        "modules": receipt["modules"],
        "checks": checks,
    }
