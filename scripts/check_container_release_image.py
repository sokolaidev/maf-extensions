"""Run profile-specific consumer checks against an immutable release image identity."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import bicep_avm_qualification
import bicep_qualification
import opentofu_random_qualification
import terraform_avm_qualification
import terraform_random_qualification
from build_hyperlight_aks_image import _smoke_output, verify_image
from container_release import ROOT, now, profiles, read, require_digest, write


def check(profile: str, image_id: str, directory: Path) -> dict[str, Any]:
    """Qualify the selected profile; controlled fixtures do not establish deployment support."""
    require_digest(image_id)
    if profile not in profiles():
        raise ValueError("Unknown image profile")
    if profile == "antivirus":
        import antivirus_qualification

        result = asyncio.run(antivirus_qualification.check(image_id))
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "offline-sdk-consumer",
            "passed": True,
            "antivirus": result,
            "containersRemaining": result["containersRemaining"],
            "liveDeploymentVerified": False,
        }
    if profile == "azure-pricing":
        import check_live_azure_pricing

        result = asyncio.run(check_live_azure_pricing.check(image_id))
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "live-public-api-sdk-consumer",
            "passed": True,
            "azurePricing": result,
            "containersRemaining": result["containersRemaining"],
            "liveDeploymentVerified": False,
        }
    if profile == "document":
        import document_qualification

        result = asyncio.run(document_qualification.check(image_id))
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "offline-sdk-consumer",
            "passed": True,
            "document": result,
            "containersRemaining": result["containersRemaining"],
            "liveDeploymentVerified": False,
        }
    if profile == "egress-proxy":
        import egress_proxy_qualification

        result = asyncio.run(egress_proxy_qualification.check(image_id))
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "controlled-docker-consumer",
            "passed": True,
            "egressProxy": result,
            "containersRemaining": result["containersRemaining"],
            "liveDeploymentVerified": False,
        }
    if profile in {"drawio", "drawio-export"}:
        import drawio_export_qualification
        import drawio_qualification

        checker = drawio_qualification if profile == "drawio" else drawio_export_qualification
        result = asyncio.run(checker.check(image_id))
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "offline-sdk-consumer",
            "passed": True,
            "drawio": result,
            "containersRemaining": result["containersRemaining"],
            "liveDeploymentVerified": False,
        }
    if profile == "hyperlight-aks-probe":
        from container_release import digest

        inputs = directory / "hyperlight-build-inputs.json"
        result = verify_image(image_id, digest(inputs).removeprefix("sha256:"))
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "packaging",
            "passed": True,
            "result": result,
            "containersRemaining": [],
            "hypervisorExecutionVerified": False,
            "liveDeploymentVerified": False,
        }
    if profile == "bicep-docker-sbx":
        import bicep_docker_sbx_qualification

        result = asyncio.run(bicep_docker_sbx_qualification.check(image_id))
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "docker-sbx-consumer",
            "passed": True,
            "bicep": result,
            "sandboxesRemaining": result["sandboxesRemaining"],
            "liveDeploymentVerified": False,
        }
    if profile in {"bicep", "bicep-avm"}:
        command = (
            bicep_qualification.command()
            if profile == "bicep"
            else bicep_avm_qualification.command()
        )
    elif profile == "shell":
        command = [
            "/usr/local/bin/python3",
            "-c",
            (ROOT / "images/shell-sandbox/test_runner.py").read_text(encoding="utf-8"),
        ]
    elif profile == "graphviz":
        command = [
            "sh",
            "-ec",
            "printf 'digraph { a -> b }' | dot -Tpng -o /tmp/render.png; test -s /tmp/render.png",
        ]
    elif profile in {"terraform-avm", "opentofu-avm"}:
        command = terraform_avm_qualification.command(profile.split("-")[0])
    elif profile == "terraform-random":
        command = terraform_random_qualification.command()
    elif profile == "opentofu-random":
        command = opentofu_random_qualification.command()
    elif profile.startswith(("terraform-", "opentofu-")):
        command = [
            "/usr/local/bin/python3",
            "-c",
            (ROOT / "images/terraform-sandbox/test_runner.py").read_text(encoding="utf-8"),
        ]
    else:
        raise ValueError("No runtime qualification for this profile")
    name = "maf-release-check-" + uuid4().hex
    create = [
        "docker",
        "create",
        "--name",
        name,
        "--log-driver",
        "none",
        "--pull=never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "--cpus",
        "2",
        "--tmpfs",
        "/tmp:rw,exec,size=512m",  # nosec B108 # Private container tmpfs.
        "--workdir",
        "/tmp",  # nosec B108 # Workdir in the private container tmpfs.
        "--entrypoint",
        command[0],
        image_id,
        *command[1:],
    ]
    if profile in {"bicep", "bicep-avm"}:
        create[2:2] = ["--env", "DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/dotnet-bundle"]
    if profile == "bicep":
        create[2:2] = ["--tmpfs", f"{bicep_qualification.WORK}:rw,noexec,size=16m"]
    subprocess.run(create, check=True, capture_output=True, timeout=30)
    try:
        start = ["docker", "start", "--attach", name]
        output = asyncio.run(
            _smoke_output(start, timeout=360)
            if profile in {"terraform-avm", "opentofu-avm", "opentofu-random", "terraform-random"}
            else _smoke_output(start)
        )
        result = None
        evidence_key = "bicepAvm" if profile == "bicep-avm" else "terraformAvm"
        if profile == "bicep":
            evidence_key = "bicep"
            result = bicep_qualification.evidence(output)
        elif profile == "bicep-avm":
            result = bicep_avm_qualification.evidence(output)
        elif profile in {"terraform-avm", "opentofu-avm"}:
            evidence_key = "terraformAvm" if profile == "terraform-avm" else "opentofuAvm"
            result = terraform_avm_qualification.evidence(output, profile.split("-")[0])
        elif profile == "terraform-random":
            evidence_key = "terraformRandom"
            result = terraform_random_qualification.evidence(output)
        elif profile == "opentofu-random":
            evidence_key = "opentofuRandom"
            result = opentofu_random_qualification.evidence(output)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name], check=True, capture_output=True, timeout=30
        )
    if profile in {
        "bicep",
        "bicep-avm",
        "terraform-avm",
        "opentofu-avm",
        "opentofu-random",
        "terraform-random",
        "shell",
    }:
        remaining = subprocess.run(
            ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if remaining:
            raise ValueError(f"{profile} qualification left its container behind")
    if profile == "shell":
        import shell_qualification

        result = asyncio.run(shell_qualification.check(image_id))
        result["analyzerFixturesPassed"] = True
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "offline-sdk-consumer",
            "passed": True,
            "shell": result,
            "containersRemaining": result["containersRemaining"],
            "liveDeploymentVerified": False,
        }
    return {
        "imageId": image_id,
        "profile": profile,
        "checkedAt": now(),
        **({evidence_key: result, "containersRemaining": []} if result is not None else {}),
        "kind": "offline-packaging",
        "passed": True,
        "liveDeploymentVerified": False,
    }


def main() -> None:
    """Record qualification of the selected image, never a mutable tag."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    candidate = read(args.candidate)
    result = check(candidate["profile"], candidate["imageId"], args.directory)
    write(args.directory / "runtime.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
