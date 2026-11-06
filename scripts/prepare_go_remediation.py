"""Prepare Go remediation files and rebuilt provider locks outside the source checkout."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import tempfile
from pathlib import Path
from typing import Any

from container_release import ROOT, read, write
from plan_go_remediation import CONTEXT, RECIPE, digest, inputs, plan
from terraform_dependencies import load_manifest, verify_built_provider
from terraform_manifest import render


def rebuild(provider: dict[str, Any], work: Path) -> tuple[bytes, dict[str, str]]:
    """Use the existing authenticated source builder and its pinned local Go toolchain."""
    spec = importlib.util.spec_from_file_location(
        "remediation_provider_builder", ROOT / CONTEXT / "build_providers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(provider, work)


def prepare(root: Path, directories: list[Path], expected: dict[str, Any], output: Path) -> None:
    """Recompute the plan and publish complete proposals only after every affected lock verifies."""
    if plan(root, directories) != expected:
        raise ValueError("Remediation plan is stale or modified")
    destination = output.resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("Proposal output must not already exist")
    if destination.is_relative_to(root.resolve()) or root.resolve().is_relative_to(destination):
        raise ValueError("Proposal output must be outside the source checkout")
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="go-remediation-", dir=destination.parent) as folder:
        staging = Path(folder)
        payload = staging / "proposal"
        payload.mkdir()
        built: dict[str, tuple[bytes, dict[str, str]]] = {}
        provider_records = []
        for path, index in sorted(changed_providers):
            provider = proposed[path]["providers"][index]
            source = provider["source_build"]
            key = json.dumps(provider, sort_keys=True)
            if key not in built:
                work = staging / f"build-{len(built)}"
                work.mkdir()
                built[key] = rebuild(provider, work)
            data, module_files = built[key]
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
                "qualification": "pending-image-build-and-rescan",
            },
        )
        payload.rename(destination)


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
