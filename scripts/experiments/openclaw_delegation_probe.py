"""Characterize a clean OpenClaw installation; never acquire real authority.

Exit zero means the probe completed, including a measured defer decision. It does
not mean a credential adapter is supported. Installation is a separate operator step.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

VERSION = "2026.10.3"
INTEGRITY = (
    "sha512-ncl6LntMlf76K39LFhVrDXQTbaGNAG5j52f7PL5P8vIn0knWmCrvfAe9DHMslFq+"
    "47HeOWBH2CBYQH8zEb+UAA=="
)
SUBPATH = "./plugin-sdk/secret-egress-runtime"
DOC = "docs/plugins/sdk-subpaths.md"
ROOT = Path(__file__).resolve().parents[2]


class ProbeRefused(RuntimeError):
    """A bounded refusal reason suitable for retained evidence."""


def digest(path: Path) -> str:
    """Hash one evidence artifact without retaining its contents."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_files(archive: Path, package: Path) -> dict[str, Any]:
    """Bind installed package bytes to the pinned npm artifact, without extracting it."""
    with archive.open("rb") as stream:
        actual = base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode()
    if "sha512-" + actual != INTEGRITY:
        raise ProbeRefused("archive_integrity_mismatch")
    count = total = 0
    tree = hashlib.sha256()
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            relative = PurePosixPath(member.name)
            if relative.parts[:1] != ("package",) or ".." in relative.parts:
                raise ProbeRefused("invalid_archive_path")
            if member.isdir():
                continue
            if not member.isfile():
                raise ProbeRefused("unsupported_archive_entry")
            count += 1
            total += member.size
            if count > 20000 or total > 400_000_000:
                raise ProbeRefused("archive_limit")
            name = PurePosixPath(*relative.parts[1:]).as_posix()
            target = package / name
            if not target.resolve().is_relative_to(package.resolve()) or not target.is_file():
                raise ProbeRefused("installed_file_missing_or_outside_package")
            stream = source.extractfile(member)
            if stream is None:
                raise ProbeRefused("archive_file_unreadable")
            with stream:
                hashed = hashlib.sha256()
                while block := stream.read(1024 * 1024):
                    hashed.update(block)
                expected = hashed.hexdigest()
            if digest(target) != expected:
                raise ProbeRefused("installed_file_mismatch")
            tree.update(f"{name}\0{expected}\n".encode())
    if not count:
        raise ProbeRefused("empty_archive")
    return {
        "files_verified": count,
        "bytes_verified": total,
        "archive_order_tree_sha256": tree.hexdigest(),
    }


def supported_node(version: str) -> bool:
    """Apply the assessed release's Node engine range."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return False
    parts = tuple(map(int, match.groups()))
    return (24, 16, 0) <= parts < (25, 0, 0) or parts >= (26, 1, 0)


def admission_reason(metadata: dict[str, Any], documentation: str) -> str:
    """Neither a new export nor changed prose automatically establishes support."""
    if metadata.get("name") != "openclaw" or metadata.get("version") != VERSION:
        return "unqualified_package"
    exports = metadata.get("exports")
    if not isinstance(exports, dict) or SUBPATH not in exports:
        return "missing_export"
    rows = [
        line for line in documentation.splitlines() if "`plugin-sdk/secret-egress-runtime`" in line
    ]
    if len(rows) == 1 and "not a third-party plugin API" in rows[0]:
        return "private_official_plugin_api"
    return "support_not_established"


def admit(metadata: dict[str, Any], documentation: str, acquire: Callable[[], object]) -> None:
    """Fence the synthetic owner until a supported authority contract is qualified."""
    raise ProbeRefused(admission_reason(metadata, documentation))


def _environment(home: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "appdata"),
            "LOCALAPPDATA": str(home / "local"),
            "TEMP": str(home),
            "TMP": str(home),
            "OPENCLAW_STATE_DIR": str(home / "state"),
            "OPENCLAW_CONFIG_PATH": str(home / "config.json"),
            "NODE_DISABLE_COMPILE_CACHE": "1",
        }
    )
    return environment


def observe_imports(project: Path, node: str, home: Path) -> dict[str, Any]:
    """Import SDK facades with a clean environment and observed side-effect guards."""
    code = Path(__file__).with_name("openclaw_delegation_import.mjs").read_text("utf-8")
    try:
        result = subprocess.run(
            [node, "--input-type=module", "-"],
            input=code,
            cwd=project,
            env=_environment(home),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProbeRefused("import_timeout") from None
    if result.returncode:
        raise ProbeRefused("import_process_failed")
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ProbeRefused("invalid_import_report") from None
    if not isinstance(observed, dict):
        raise ProbeRefused("invalid_import_report")
    observed["isolated_home_unchanged"] = not any(home.iterdir())
    return observed


def probe(project: Path, archive: Path, node: str) -> dict[str, Any]:
    """Verify installed bytes and record a bounded admission refusal."""
    project = project.resolve()
    if project.is_relative_to(ROOT) or any(
        (parent / ".git").exists() for parent in (project, *project.parents)
    ):
        raise ProbeRefused("installation_must_be_outside_checkout")
    package = project / "node_modules" / "openclaw"
    if not package.resolve().is_relative_to(project):
        raise ProbeRefused("installed_package_outside_project")
    verified = verify_files(archive, package)
    metadata = json.loads((package / "package.json").read_text("utf-8"))
    documentation = (package / DOC).read_text("utf-8")
    lock = json.loads((project / "package-lock.json").read_text("utf-8"))
    locked = lock.get("packages", {}).get("node_modules/openclaw", {})
    if locked.get("version") != VERSION or locked.get("integrity") != INTEGRITY:
        raise ProbeRefused("install_lock_mismatch")
    with tempfile.TemporaryDirectory(prefix="maf-delegation-home-") as directory:
        home = Path(directory)
        version = subprocess.run(
            [node, "--version"],
            cwd=project,
            env=_environment(home),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        if not supported_node(version):
            raise ProbeRefused("unsupported_node")
        imports = observe_imports(project, node, home)
    if imports.get("side_effect_attempts") or not imports.get("isolated_home_unchanged"):
        raise ProbeRefused("discovery_side_effect")
    observations = imports.get("imports", [])
    if len(observations) != 2 or observations[0].get("status") != "imported":
        raise ProbeRefused("public_import_control_failed")
    if "definePluginEntry" not in observations[0].get("exports", []):
        raise ProbeRefused("public_import_control_failed")
    owner_requests: list[str] = []

    def synthetic_owner() -> object:
        owner_requests.append("acquire")
        return {"token": "synthetic-only-not-a-credential"}

    try:
        admit(metadata, documentation, synthetic_owner)
    except ProbeRefused as refusal:
        reason = str(refusal)
    else:
        raise ProbeRefused("unexpected_admission")
    return {
        "schema_version": 1,
        "decision": "defer",
        "reason": reason,
        "scope": "installed_import_and_admission_probe",
        "artifact": {
            "name": metadata["name"],
            "version": metadata["version"],
            "npm_integrity": INTEGRITY,
            "archive_sha256": digest(archive),
            "package_json_sha256": digest(package / "package.json"),
            "sdk_documentation_sha256": digest(package / DOC),
            "package_lock_sha256": digest(project / "package-lock.json"),
            **verified,
        },
        "runtime": {"node": version, "os": platform.system(), "machine": platform.machine()},
        "target_export": metadata.get("exports", {}).get(SUBPATH),
        "discovery": imports,
        "synthetic_owner_requests": len(owner_requests),
        "credential_runtime_exercised": False,
        "sandbox_provisioned": False,
        "unqualified": [
            "authenticated_authority_and_approval_binding",
            "credential_selection_and_confinement",
            "finite_expiry_and_orphan_cutoff",
            "revocation_cancellation_and_streams",
            "handle_replay_and_restart",
            "final_dial_address_policy",
            "installed_gateway_plugin_registration",
        ],
    }


def main() -> int:
    """Write a report only when the installed probe completes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--node", default="node")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = probe(args.project, args.archive, args.node)
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        tarfile.TarError,
        ProbeRefused,
    ) as error:
        reason = str(error) if isinstance(error, ProbeRefused) else type(error).__name__
        print(json.dumps({"probe_completed": False, "reason": reason}))
        return 1
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"probe_completed": True, "decision": report["decision"], "reason": report["reason"]}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
