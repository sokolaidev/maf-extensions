"""Qualify an exact Bicep image through a local Docker Sandboxes template and SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import subprocess
import tarfile
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any
from uuid import uuid4

import bicep_qualification as bicep
from container_release import ROOT, require_digest
from maf_sandbox import SandboxKey, SandboxSpec
from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig


def require(condition: bool, message: str) -> None:
    """Keep release checks enabled under optimized Python."""
    if not condition:
        raise ValueError(message)


def command(*args: str, timeout: float = 60) -> str:
    """Bound CLI operations and retain diagnostics on failure."""
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        raise ValueError(f"{args[0]} {args[1]} failed: {result.stderr[-4000:]}")
    return result.stdout.strip()


def verify_archive(path: Path, image_id: str, tag: str) -> None:
    """Bind the imported template tag to the selected Docker configuration digest."""
    with tarfile.open(path) as archive:
        manifest = archive.extractfile("manifest.json")
        require(manifest is not None, "Template archive has no Docker manifest")
        assert manifest is not None
        entries = json.load(manifest)
        require(len(entries) == 1 and entries[0]["RepoTags"] == [tag], "Unexpected template tags")
        config = archive.extractfile(entries[0]["Config"])
        require(config is not None, "Template archive has no image configuration")
        assert config is not None
        raw = config.read()
        require("sha256:" + hashlib.sha256(raw).hexdigest() == image_id, "Template image drift")
        value = json.loads(raw)
        require(
            value["os"] == "linux" and value["architecture"] == "amd64",
            "Template requires Linux/amd64",
        )


def sandboxes(sbx: str) -> list[dict[str, Any]]:
    """Refuse unknown listing shapes instead of claiming cleanup succeeded."""
    value = json.loads(command(sbx, "ls", "--json"))
    rows = value.get("sandboxes") if isinstance(value, dict) else value
    require(
        isinstance(rows, list)
        and all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in rows),
        "SBX did not return a sandbox list",
    )
    assert isinstance(rows, list)
    return rows


async def check(image_id: str) -> dict[str, Any]:
    """Load only the selected bytes, exercise the backend and refuse incomplete cleanup."""
    require_digest(image_id)
    sbx = os.environ.get("SBX_PATH", "sbx")
    cli_version = await asyncio.to_thread(command, sbx, "version")
    scope = "bicep-release-" + uuid4().hex
    tag = "maf-bicep-qualification:" + uuid4().hex
    key = SandboxKey(scope, "compiler", "release")
    workspace = Path(mkdtemp(prefix="maf-bicep-sbx-"))
    backend = SbxSandboxBackend(SbxSandboxConfig(sbx_path=sbx, workspace_root=workspace))
    loaded = False
    tagged = False
    sandbox_name: str | None = None
    result: dict[str, Any] = {}
    try:
        with TemporaryDirectory(prefix="maf-bicep-template-") as temporary:
            archive = Path(temporary) / "template.tar"
            await asyncio.to_thread(command, "docker", "tag", image_id, tag)
            tagged = True
            await asyncio.to_thread(
                command, "docker", "save", "--output", str(archive), tag, timeout=300
            )
            await asyncio.to_thread(verify_archive, archive, image_id, tag)
            await asyncio.to_thread(command, sbx, "template", "load", str(archive), timeout=600)
            loaded = True
        sandbox = await backend.acquire(key, SandboxSpec(kind="bicep-release", image=tag))
        sandbox_name = sandbox.name
        rows = await asyncio.to_thread(sandboxes, sbx)
        own = [row for row in rows if row["name"] == sandbox.name]
        require(
            len(own) == 1 and own[0].get("workspaces") == [str(sandbox.mount.host)],
            "SBX workspace attachment differs from the backend's workspace",
        )
        packaging = await sandbox.exec(
            [
                "sh",
                "-ec",
                "test ! -e bicepconfig.json; bash --version; unshare --version; sha256sum /licenses/maf-extensions-LICENSE",
            ],
            working_directory=".",
            timeout=30,
        )
        require(packaging.exit_code == 0, "SBX image utilities, notices or workspace layout failed")
        notice = hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
        require(packaging.stdout.splitlines()[-1].split()[0] == notice, "Suite license differs")
        await sandbox.write_file(
            "bicepconfig.json",
            (bicep.CONTEXT / "bicepconfig.json").read_bytes(),
            working_directory=".",
        )
        token = uuid4().hex
        await sandbox.write_file("release-probe/input.txt", token, working_directory=".")
        transfer = await sandbox.exec(
            [
                "sh",
                "-ec",
                "cat release-probe/input.txt; cp release-probe/input.txt release-probe/output.txt",
            ],
            working_directory=".",
            timeout=30,
        )
        returned = await sandbox.read_file(
            "release-probe/output.txt", working_directory=".", max_bytes=128
        )
        require(
            transfer.exit_code == 0 and transfer.stdout == token and returned == token.encode(),
            "SBX workspace round trip failed",
        )
        probe = await sandbox.exec(bicep.command(), working_directory=".", timeout=180)
        require(probe.exit_code == 0, f"SBX compiler probes failed: {probe.stderr[-2000:]}")
        result = bicep.evidence(probe.stdout_bytes)
        template = await sandbox.read_file(
            "release-probe/valid-local/main.json", working_directory=".", max_bytes=65536
        )
        require(
            json.loads(template) == json.loads(probe.stdout)["checks"][0]["template"],
            "SBX compiler output transfer differs",
        )
        for item in result["checks"]:
            if item["name"] == "fallback-config":
                item["name"] = "staged-strict-config"
        result.update(
            {
                "backend": "maf-sandbox-docker-sbx",
                "backendVersion": version("maf-sandbox-docker-sbx"),
                "coreVersion": version("maf-sandbox"),
                "sdkSource": "release-source-checkout",
                "sbxVersion": cli_version,
                "host": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                },
                "network": "sbx-deny-all",
                "configuration": "staged-through-sdk",
                "template": {"imageId": image_id, "tag": tag, "archiveVerified": True},
                "workspaceRoundTrip": True,
                "compiledOutputTransfer": True,
                "suiteLicenseSha256": notice,
                "utilities": packaging.stdout.splitlines()[:-1],
            }
        )
    finally:
        purge = await backend.dispose_scope(scope, "compiler")
        require(not purge.undisposed, "SBX scope cleanup failed; workspace retained")
        remaining = await asyncio.to_thread(sandboxes, sbx)
        require(
            not any(
                row["name"] == sandbox_name
                or any(Path(path).is_relative_to(workspace) for path in row.get("workspaces", []))
                for row in remaining
            ),
            "SBX qualification left a workspace attached",
        )
        require(not any(workspace.iterdir()), "SBX qualification left workspace files")
        workspace.rmdir()
        if loaded:
            await asyncio.to_thread(command, sbx, "template", "rm", tag)
        if tagged:
            await asyncio.to_thread(command, "docker", "image", "rm", tag)
    return result | {"sandboxesRemaining": [], "workspaceRemoved": True, "templateRemoved": True}
