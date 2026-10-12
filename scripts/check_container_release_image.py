"""Run offline packaging checks against an immutable release image identity."""

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
import terraform_avm_qualification
from build_hyperlight_aks_image import _smoke_output, verify_image
from container_release import ROOT, now, profiles, read, require_digest, write


def check(profile: str, image_id: str, directory: Path) -> dict[str, Any]:
    """Qualify packaging without network access; this does not establish live deployment support."""
    require_digest(image_id)
    if profile not in profiles():
        raise ValueError("Unknown image profile")
    if profile == "hyperlight":
        from container_release import digest

        inputs = directory / "hyperlight-build-inputs.json"
        result = verify_image(image_id, digest(inputs).removeprefix("sha256:"))
        return {
            "imageId": image_id,
            "profile": profile,
            "checkedAt": now(),
            "kind": "packaging",
            "result": result,
        }
    if profile in {"bicep", "bicep-avm", "sbx-bicep"}:
        command = [
            "sh",
            "-ec",
            "bicep --version; printf \"param name string = 'release-probe'\\noutput result string = name\\n\" > /tmp/main.bicep; bicep build /tmp/main.bicep --outfile /tmp/main.json; test -s /tmp/main.json",
        ]
        if profile == "bicep":
            command = bicep_qualification.command()
        elif profile == "bicep-avm":
            command = bicep_avm_qualification.command()
    elif profile in {"graphviz", "drawio-sandbox"}:
        command = [
            "sh",
            "-ec",
            "printf 'digraph { a -> b }' | dot -Tpng -o /tmp/render.png; test -s /tmp/render.png",
        ]
    elif profile == "drawio-export":
        command = [
            "python3",
            "-c",
            "import json,runpy,time; from pathlib import Path; export=runpy.run_path('/opt/maf-drawio/export.py')['export_document']; xml='<mxfile><diagram><mxGraphModel><root><mxCell id=\"0\"/><mxCell id=\"1\" parent=\"0\"/><mxCell id=\"2\" parent=\"1\" vertex=\"1\" value=\"Smoke\"><mxGeometry x=\"10\" y=\"10\" width=\"120\" height=\"60\" as=\"geometry\"/></mxCell></root></mxGraphModel></diagram></mxfile>'; export(xml, {'pages': [], 'formats': ['png', 'svg', 'jpg'], 'scale': 1, 'jpeg_quality': 90, 'transparent': False}, time.monotonic()+45); assert json.loads(Path('exports.json').read_text())['files']==['diagram-1.png','diagram-1.svg','diagram-1.jpg']; assert Path('diagram-1.png').read_bytes().startswith(bytes.fromhex('89504e470d0a1a0a'))",
        ]
    elif profile == "terraform-avm":
        command = terraform_avm_qualification.command()
    elif profile.startswith(("terraform-", "opentofu-")):
        command = [
            "/usr/local/bin/python3",
            "-c",
            (ROOT / "images/terraform-sandbox/test_runner.py").read_text(encoding="utf-8"),
        ]
    else:
        command = [
            "sh",
            "-ec",
            "test -x /usr/local/bin/iron-proxy; test -x /entrypoint.sh; iron-proxy generate-ca --outdir /tmp; test -s /tmp/ca.crt",
        ]
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
        "/tmp:rw,exec,size=512m",
        "--workdir",
        "/tmp",
        "--entrypoint",
        command[0],
        image_id,
        *command[1:],
    ]
    if profile in {"bicep", "bicep-avm", "sbx-bicep"}:
        create[2:2] = ["--env", "DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/dotnet-bundle"]
    if profile == "bicep":
        create[2:2] = ["--tmpfs", f"{bicep_qualification.WORK}:rw,noexec,size=16m"]
    if profile == "drawio-export":
        for capability in ("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"):
            create[2:2] = ["--cap-add", capability]
    subprocess.run(create, check=True, capture_output=True, timeout=30)
    try:
        start = ["docker", "start", "--attach", name]
        output = asyncio.run(
            _smoke_output(start, timeout=360)
            if profile == "terraform-avm"
            else _smoke_output(start)
        )
        result = None
        evidence_key = "bicepAvm" if profile == "bicep-avm" else "terraformAvm"
        if profile == "bicep":
            evidence_key = "bicep"
            result = bicep_qualification.evidence(output)
        elif profile == "bicep-avm":
            result = bicep_avm_qualification.evidence(output)
        elif profile == "terraform-avm":
            result = terraform_avm_qualification.evidence(output)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name], check=True, capture_output=True, timeout=30
        )
    if profile in {"bicep", "bicep-avm", "terraform-avm"}:
        remaining = subprocess.run(
            ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if remaining:
            raise ValueError(f"{profile} qualification left its container behind")
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
