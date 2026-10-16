"""Offline release probes for the base Bicep compiler and configuration."""

from __future__ import annotations

import hashlib
import json
import shlex
from typing import Any

from bicep_image_pin import read_pin
from bicep_source import receipt_command, verify_receipt
from container_release import ROOT

CONTEXT = ROOT / "images/bicep-sandbox"
WORK = "/maf-sandbox/work/release-probe"
NOTICE = "/usr/share/licenses/bicep/LICENSE"


def cases() -> list[dict[str, Any]]:
    """Exercise local validation, configuration discovery and the absent module cache."""
    unused = "param unused string = 'unused'\n"
    return [
        {
            "name": "valid-local",
            "source": "param name string = 'release-probe'\noutput result string = name\n",
            "errors": [],
            "warnings": [],
        },
        {
            "name": "invalid-type",
            "source": "output result string = 42\n",
            "errors": ["BCP033"],
            "warnings": [],
        },
        {
            "name": "fallback-config",
            "source": unused,
            "errors": ["no-unused-params"],
            "warnings": [],
        },
        {
            "name": "default-config-control",
            "source": unused,
            "errors": [],
            "warnings": ["no-unused-params"],
        },
        {"name": "staged-config", "source": unused, "errors": [], "warnings": []},
        {
            "name": "uncached-module",
            "source": "module missing 'br/public:avm/res/network/virtual-network:0.0.0' = {\n  name: 'missing'\n}\n",
            "errors": ["BCP190"],
            "warnings": [],
        },
    ]


def identities() -> dict[str, str]:
    """Bind the installed compiler, fallback policy and notice to the selected source."""
    _, compiler = read_pin(CONTEXT / "Dockerfile")
    return {
        "compilerSha256": compiler,
        "configSha256": hashlib.sha256((CONTEXT / "bicepconfig.json").read_bytes()).hexdigest(),
        "licenseSha256": hashlib.sha256((CONTEXT / "BICEP-LICENSE").read_bytes()).hexdigest(),
    }


def command() -> list[str]:
    """Run direct compiler checks beneath the baked fallback and outside it as a control."""
    lines = [
        "set -eu",
        'version="$(bicep --version)"',
        'printf \'{"bicepVersion":"%s"\' "$version"',
    ]
    for key, path in (
        ("compilerSha256", "/usr/local/bin/bicep"),
        ("configSha256", "/maf-sandbox/work/bicepconfig.json"),
        ("licenseSha256", NOTICE),
    ):
        lines.extend(
            [
                f"hash=$(sha256sum {path})",
                f'printf \',"{key}":"%s"\' "${{hash%% *}}"',
            ]
        )
    lines.append(receipt_command())
    lines.append("printf ',\"checks\":['")
    for index, case in enumerate(cases()):
        name = case["name"]
        parent = "/tmp" if name == "default-config-control" else WORK
        lines.extend([f"mkdir {parent}/{name}", f"cd {parent}/{name}", "export HOME=$PWD"])
        if name == "staged-config":
            config = json.dumps(
                {"analyzers": {"core": {"rules": {"no-unused-params": {"level": "off"}}}}}
            )
            lines.append(f"printf %s {shlex.quote(config)} > bicepconfig.json")
        lines.extend(
            [
                f"printf %s {shlex.quote(case['source'])} > main.bicep",
                "status=0",
                "bicep build main.bicep --no-restore --diagnostics-format sarif --outfile main.json > stdout.txt 2> diagnostics.json || status=$?",
                f'printf \'{"," if index else ""}{{"name":"{name}","exitCode":%s,"diagnostics":\' "$status"',
                "cat diagnostics.json",
                "printf ',\"template\":'",
                "if test -e main.json; then cat main.json; else printf null; fi",
                "printf '}'",
            ]
        )
    lines.append("printf ']}'")
    return ["sh", "-ec", "\n".join(lines)]


def evidence(output: bytes) -> dict[str, Any]:
    """Refuse mismatched payloads, unexpected diagnostics and missing compiled templates."""
    result = json.loads(output)
    version, _ = read_pin(CONTEXT / "Dockerfile")
    if not result["bicepVersion"].startswith(f"Bicep CLI version {version.removeprefix('v')} "):
        raise ValueError("Bicep compiler version differs from the selected source")
    build = verify_receipt(result.get("buildReceipt"), result["compilerSha256"], CONTEXT)
    expected_identities = identities() | {"compilerSha256": build["compilerSha256"]}
    for key, expected in expected_identities.items():
        if result[key] != expected:
            raise ValueError(f"Bicep payload differs from the selected source: {key}")
    observed = result["checks"]
    expected_cases = cases()
    if not isinstance(observed, list) or len(observed) != len(expected_cases):
        raise ValueError("Bicep qualification cases are incomplete")
    checks = []
    for actual, case in zip(observed, expected_cases, strict=True):
        runs = actual["diagnostics"]["runs"]
        diagnostics = {
            level: sorted(
                {
                    item["ruleId"]
                    for run in runs
                    for item in run["results"]
                    if item.get("level", "warning") == level
                }
            )
            for level in ("error", "warning")
        }
        template = actual["template"]
        valid_template = (
            isinstance(template, dict)
            and template.get("$schema", "").endswith("deploymentTemplate.json#")
            and template.get("resources") == []
        )
        if (
            actual["name"] != case["name"]
            or not runs
            or diagnostics["error"] != case["errors"]
            or diagnostics["warning"] != case["warnings"]
            or actual["exitCode"] != (1 if case["errors"] else 0)
            or (template is not None if case["errors"] else not valid_template)
        ):
            raise ValueError(f"Bicep consumer probe failed: {case['name']}")
        if case["name"] == "valid-local" and (
            template.get("parameters", {}).get("name", {}).get("defaultValue") != "release-probe"
            or template.get("outputs", {}).get("result")
            != {"type": "string", "value": "[parameters('name')]"}
        ):
            raise ValueError("Bicep local template does not preserve the input")
        checks.append(
            {
                "name": case["name"],
                "passed": True,
                **diagnostics,
                "outputPresent": template is not None,
            }
        )
    return {
        "bicepVersion": result["bicepVersion"],
        **expected_identities,
        "build": build,
        "backend": "docker-cli",
        "network": "none",
        "checks": checks,
    }
