"""Qualify native Draw.io exports and their Docker boundary before image publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import check_drawio_exports
import drawio_qualification as editable
from container_release import ROOT, require_digest
from maf_sandbox import SandboxKey, SandboxSpec
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL")


def observation(scope: str, image_id: str) -> dict[str, Any]:
    """Check the actual container's network, capabilities, mounts and resource limits."""
    result = editable.observation(scope, image_id)
    value = json.loads(editable.docker("inspect", result["containerId"]))[0]
    host = value["HostConfig"]

    def normalize(values: list[str] | None) -> set[str]:
        return {v.removeprefix("CAP_").upper() for v in (values or [])}

    editable.require(normalize(host["CapDrop"]) == {"ALL"}, "Export requires drop-all")
    editable.require(normalize(host["CapAdd"]) == set(CAPABILITIES), "Wrong export capabilities")
    editable.require(
        host["Privileged"] is False
        and "no-new-privileges" in host["SecurityOpt"]
        and not host.get("Devices")
        and not host.get("DeviceRequests")
        and host.get("PidMode", "") == ""
        and host.get("IpcMode") != "host",
        "Export container has an unsafe outer boundary",
    )
    editable.require(
        host["Memory"] == 1024**3
        and host["NanoCpus"] == 2_000_000_000
        and host["PidsLimit"] == 256,
        "Export resource limits differ from qualification policy",
    )
    editable.require(
        all(mount["Type"] == "tmpfs" for mount in value["Mounts"]),
        "Export container exposes a host or persistent mount",
    )
    return {
        **result,
        "capDrop": sorted(normalize(host["CapDrop"])),
        "capAdd": sorted(normalize(host["CapAdd"])),
        "noNewPrivileges": True,
        "memoryBytes": host["Memory"],
        "nanoCpus": host["NanoCpus"],
        "pidsLimit": host["PidsLimit"],
        "mounts": value["Mounts"],
    }


async def check(image_id: str) -> dict[str, Any]:
    """Run the existing editable and native-export suites against one immutable image."""
    require_digest(image_id)
    editable_report = await editable.check(image_id)
    observations: list[dict[str, Any]] = []
    runtime: dict[str, Any] = {}
    scopes: set[tuple[str, str]] = set()

    class Backend(DockerSandboxBackend):
        async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Any:
            scopes.add((key.scope, key.thread_id))
            sandbox = await super().acquire(key, spec)
            observations.append(
                {
                    **await asyncio.to_thread(observation, key.scope, image_id),
                    "callId": key.call_id,
                }
            )
            if not runtime:
                probe = await sandbox.exec(
                    [
                        "python3",
                        "-c",
                        (ROOT / "scripts/drawio_export_runtime.py").read_text("utf-8"),
                    ],
                    timeout=30,
                    working_directory="/tmp",
                )
                editable.require(probe.exit_code == 0, "Export runtime inventory failed")
                runtime.update(json.loads(probe.stdout))
            return sandbox

    backend = await Backend.create(
        DockerSandboxConfig(memory="1g", cpus=2, pids_limit=256, cap_add=CAPABILITIES)
    )
    try:
        with TemporaryDirectory(prefix="drawio-export-release-") as temporary:
            destination = Path(temporary)
            report: Any = await check_drawio_exports.check(
                image_id, destination, backend, emit=False
            )
            editable.require(
                report["renderer_identity"]["caller_uid"] == 0, "Wrong coordinator UID"
            )
            editable.require(
                len({item["callId"] for item in observations}) == report["clean_calls"] + 1,
                "Export calls lack engine observations",
            )
            artifacts = {
                path.relative_to(destination).as_posix(): {
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in sorted(destination.rglob("*"))
                if path.is_file()
            }
            expected = {"diagram.drawio", "stored/diagram-2.png", "stored/diagram-2.jpg"}
            expected.update(
                f"diagram-{page}.{fmt}" for page in (1, 2) for fmt in ("png", "jpg", "svg")
            )
            editable.require(set(artifacts) == expected, "Unexpected export artifacts")
            for scope, _ in scopes:
                editable.require(
                    not await asyncio.to_thread(editable.owned, scope),
                    "Export calls left containers before final cleanup",
                )
    finally:
        for scope, thread in scopes:
            purge = await backend.dispose_scope(scope, thread)
            editable.require(not purge.undisposed, "Export scope cleanup failed")
            editable.require(
                not await asyncio.to_thread(editable.owned, scope), "Export containers remain"
            )
    return {
        "backend": "maf-sandbox-docker",
        "sdkSource": "checkout",
        "packages": editable_report["packages"],
        "docker": editable_report["docker"],
        "versions": {**editable_report["versions"], **runtime},
        "editable": editable_report,
        "exports": report,
        "checks": [
            {"name": "editable-without-added-capabilities", "passed": True},
            {
                "name": "native-exports-and-refusals",
                "passed": True,
                "cleanCalls": report["clean_calls"],
            },
        ],
        "coordinatorCapabilities": list(CAPABILITIES),
        "observations": observations,
        "artifacts": artifacts,
        "containersRemaining": [],
    }


def verify_inventory(sbom: dict[str, Any], runtime: dict[str, Any]) -> None:
    """Refuse scans that omit the application and packages bundled inside its ASAR."""
    expected = runtime["drawio"]["versions"]["packageInventory"]
    pairs = {(item["name"], item["version"]) for item in expected}
    found = {(item["name"], item["version"]) for item in sbom["artifacts"]}
    editable.require(
        {"electron", "draw.io"} <= {name for name, _ in pairs},
        "Missing native application inventory",
    )
    editable.require(pairs <= found, "SBOM omits bundled application packages")
