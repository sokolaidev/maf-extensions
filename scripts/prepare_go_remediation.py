"""Prepare Go remediation files and rebuilt provider locks outside the source checkout."""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from container_release import ROOT, read, write
from plan_go_remediation import RECIPE, TOOLING, digest, inputs, plan, semver
from terraform_dependencies import load_manifest, verify_built_provider
from terraform_manifest import render

AT_FDCWD = -100
RENAME_NOREPLACE = 1


def publish(source: Path, destination: Path) -> None:
    """Atomically publish a complete directory without replacing a concurrent destination."""
    if sys.platform == "win32":
        source.rename(destination)
        return
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "Exclusive proposal publication requires Windows or Linux")
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "Exclusive proposal publication requires renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def selected_modules(requested: dict[str, str], work: Path, env: dict[str, str]) -> dict[str, str]:
    """Read requested modules from the full source graph, refusing replacements."""
    raw = subprocess.check_output(
        ["go", "list", "-m", "-json", "all"], cwd=work, env=env, text=True
    )
    resolved = {}
    decoder = json.JSONDecoder()
    while raw.strip():
        row, end = decoder.raw_decode(raw.lstrip())
        raw = raw.lstrip()[end:]
        name, version = row.get("Path"), row.get("Version")
        if name not in requested:
            continue
        if name in resolved or "Replace" in row or row.get("Main"):
            raise ValueError("Unexpected resolved Go module")
        resolved[name] = version
    return resolved


def resolve_modules(requested: dict[str, str], work: Path, env: dict[str, str]) -> dict[str, str]:
    """Resolve companion pins without lowering source requirements or crossing major versions."""
    if not requested:
        return {}
    snapshots = {
        path: path.read_bytes() if path.exists() else None
        for path in (work / "go.mod", work / "go.sum")
    }
    try:
        required = requested.copy()
        for name, version in selected_modules(requested, work, env).items():
            source, target = semver(version), semver(requested[name])
            if source[0] != target[0]:
                raise ValueError("Unsafe source module major change: " + name)
            if source > target:
                required[name] = version
        subprocess.run(
            ["go", "mod", "edit", *(f"-require={n}@{v}" for n, v in sorted(required.items()))],
            cwd=work,
            env=env,
            check=True,
        )
        resolved = selected_modules(required, work, env)
        if resolved.keys() != requested.keys():
            raise ValueError("Missing resolved Go modules")
        for name, version in resolved.items():
            before, after = semver(required[name]), semver(version)
            if after[0] != before[0] or after < before:
                raise ValueError("Unsafe companion module update: " + name)
        return resolved
    finally:
        # The ordinary builder must regenerate identical module files from original source.
        for path, raw in snapshots.items():
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(raw)


def snapshot_tooling(expected: dict[str, str], destination: Path) -> None:
    """Copy the exact verified bytes used by the isolated builder and its repository imports."""
    snapshots = {name: (ROOT / name).read_bytes() for name in TOOLING}
    if {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshots.items()} != expected:
        raise ValueError("Remediation tooling snapshot differs from the plan")
    for name, raw in snapshots.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def rebuild(provider: dict[str, Any], work: Path, tooling: Path) -> tuple[bytes, dict[str, str]]:
    """Run verified tooling in a fresh interpreter without live-checkout imports."""
    request, result, package = (
        work.parent / (work.name + suffix) for suffix in (".json", ".result.json", ".zip")
    )
    write(request, provider)
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(tooling / "scripts/go_remediation_worker.py"),
            str(request),
            str(work),
            str(result),
            str(package),
        ],
        cwd=tooling,
        check=True,
    )
    record = read(result)
    provider["source_build"]["modules"] = record["modules"]
    return package.read_bytes(), record["moduleFiles"]


def proposed_inputs(
    root: Path, expected: dict[str, Any]
) -> tuple[dict[str, Any], set[tuple[str, int]], set[str]]:
    """Apply planned pins to a copy, preserving every shared provider declaration."""
    documents, hashes = inputs(root)
    if hashes != expected["inputs"]:
        raise ValueError("Remediation source snapshot differs from the plan")
    proposed = copy.deepcopy(documents)
    changed_providers: set[tuple[str, int]] = set()
    changed = set()
    for update in expected["updates"]:
        path, owner = update["path"], update["owner"]
        if path == RECIPE:
            source = proposed[path]["engines"][owner]["source_build"]
        else:
            index = int(owner)
            source = proposed[path]["providers"][index]["source_build"]
            changed_providers.add((path, index))
        source["modules"][update["module"]] = update["after"]
        changed.add(path)
    return proposed, changed_providers, changed


def identity(value: dict[str, Any]) -> str:
    """Bind a worker result to its complete plan or provider inputs."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def provider_requests(root: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate identical provider builds across all changed manifests."""
    proposed, changed, _ = proposed_inputs(root, expected)
    providers = [proposed[path]["providers"][index] for path, index in sorted(changed)]
    return {identity(provider): provider for provider in providers}


def output_directory(root: Path, output: Path) -> Path:
    """Reserve a new output outside the source tree without replacing an existing result."""
    destination = output.resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("Proposal output must not already exist")
    if destination.is_relative_to(root.resolve()) or root.resolve().is_relative_to(destination):
        raise ValueError("Proposal output must be outside the source checkout")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def build_shard(
    root: Path, directories: list[Path], expected: dict[str, Any], shard: str, output: Path
) -> None:
    """Build one planned provider and atomically retain its package and bound result."""
    if plan(root, directories) != expected:
        raise ValueError("Remediation plan is stale or modified")
    requests = provider_requests(root, expected)
    if shard not in requests:
        raise ValueError("Unknown provider shard")
    destination = output_directory(root, output / shard)
    provider = requests[shard]
    with tempfile.TemporaryDirectory(prefix="go-remediation-", dir=destination.parent) as folder:
        staging = Path(folder)
        tooling, work, payload = (staging / name for name in ("tooling", "build", "result"))
        snapshot_tooling(expected["tooling"], tooling)
        work.mkdir()
        data, hashes = rebuild(provider, work, tooling)
        source = provider["source_build"]
        source.update(sha256=hashlib.sha256(data).hexdigest(), module_files=hashes)
        verify_built_provider(provider, data)
        payload.mkdir()
        (payload / "provider.zip").write_bytes(data)
        write(
            payload / "result.json",
            {
                "schemaVersion": 1,
                "plan": identity(expected),
                "request": shard,
                "modules": source["modules"],
                "moduleFiles": hashes,
                "sha256": source["sha256"],
            },
        )
        if plan(root, directories) != expected:
            raise ValueError("Remediation inputs changed during preparation")
        publish(payload, destination)


def read_shard(
    directory: Path, expected: dict[str, Any], provider: dict[str, Any]
) -> tuple[bytes, dict[str, str], dict[str, str]]:
    """Verify worker identity, companion pins and package bytes before assembling locks."""
    shard = identity(provider)
    folder = directory / shard
    paths = [folder, folder / "result.json", folder / "provider.zip"]
    if any(
        path.is_symlink() or not path.resolve().is_relative_to(directory.resolve())
        for path in paths
    ):
        raise ValueError("Provider shard escapes its artifact")
    if {path.name for path in folder.iterdir()} != {"result.json", "provider.zip"}:
        raise ValueError("Unexpected provider shard files")
    result = read(folder / "result.json")
    if (result.get("schemaVersion"), result.get("plan"), result.get("request")) != (
        1,
        identity(expected),
        shard,
    ):
        raise ValueError("Provider shard identifies another plan or request")
    requested = provider["source_build"]["modules"]
    resolved = result["modules"]
    if resolved.keys() != requested.keys():
        raise ValueError("Provider shard changed the module set")
    for name, version in resolved.items():
        before, after = semver(requested[name]), semver(version)
        if after[0] != before[0] or after < before:
            raise ValueError("Unsafe companion module update")
    data = (folder / "provider.zip").read_bytes()
    verified = copy.deepcopy(provider)
    verified["source_build"].update(
        modules=resolved, module_files=result["moduleFiles"], sha256=result["sha256"]
    )
    verify_built_provider(verified, data)
    return data, result["moduleFiles"], resolved


def prepare(
    root: Path,
    directories: list[Path],
    expected: dict[str, Any],
    output: Path,
    *,
    shards: Path | None = None,
) -> None:
    """Publish a complete proposal from local builds or an exact set of verified workers."""
    if plan(root, directories) != expected:
        raise ValueError("Remediation plan is stale or modified")
    destination = output_directory(root, output)
    proposed, changed_providers, changed = proposed_inputs(root, expected)
    if shards is not None:
        requests = provider_requests(root, expected)
        if shards.is_symlink() or {path.name for path in shards.iterdir()} != requests.keys():
            raise ValueError("Provider shard set differs from planned builds")
    with tempfile.TemporaryDirectory(prefix="go-remediation-", dir=destination.parent) as folder:
        staging = Path(folder)
        tooling = staging / "tooling"
        snapshot_tooling(expected["tooling"], tooling)
        payload = staging / "proposal"
        payload.mkdir()
        built: dict[str, tuple[bytes, dict[str, str], dict[str, str]]] = {}
        provider_records = []
        companion_updates = []
        for path, index in sorted(changed_providers):
            provider = proposed[path]["providers"][index]
            source = provider["source_build"]
            requested = source["modules"].copy()
            key = json.dumps(provider, sort_keys=True)
            if key not in built:
                if shards is None:
                    work = staging / f"build-{len(built)}"
                    work.mkdir()
                    data, module_files = rebuild(provider, work, tooling)
                    built[key] = data, module_files, source["modules"].copy()
                else:
                    built[key] = read_shard(shards, expected, provider)
            data, module_files, resolved = built[key]
            source["modules"] = resolved.copy()
            companion_updates.extend(
                {
                    "path": path,
                    "owner": str(index),
                    "module": name,
                    "before": requested[name],
                    "after": version,
                }
                for name, version in sorted(resolved.items())
                if requested[name] != version
            )
            source["sha256"] = hashlib.sha256(data).hexdigest()
            source["module_files"] = module_files
            verify_built_provider(provider, data)
            package = f"providers/{source['sha256']}.zip"
            (payload / "providers").mkdir(exist_ok=True)
            (payload / package).write_bytes(data)
            provider_records.append({"path": path, "index": index, "package": package})
        files = {}
        for name in sorted(changed):
            target = payload / "files" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = (render(proposed[name]) + "\n").encode()
            if name != RECIPE:
                load_manifest(raw)
            target.write_bytes(raw)
            files[name] = digest(target)
        # A long build must not publish a proposal against inputs that changed meanwhile.
        if plan(root, directories) != expected:
            raise ValueError("Remediation inputs changed during preparation")
        write(payload / "plan.json", expected)
        write(
            payload / "proposal.json",
            {
                "schemaVersion": 1,
                "files": files,
                "providers": provider_records,
                "companionUpdates": companion_updates,
                "qualification": "pending-image-build-and-rescan",
            },
        )
        publish(payload, destination)


def main() -> None:
    """Prepare proposed source files and provider packages from an unchanged plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--evidence", type=Path, action="append", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.root, args.evidence, read(args.plan), args.output)


if __name__ == "__main__":
    main()
