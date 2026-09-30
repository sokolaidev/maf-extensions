"""Lock public AVM artifacts and prepare a verified Bicep cache at image build time."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

REFERENCE = re.compile(r"br/public:(avm/res/[a-z0-9-]+/[a-z0-9-]+):([0-9]+\.[0-9]+\.[0-9]+)")
ROOT = Path(__file__).resolve().parents[1] / "images/bicep-sandbox"
LAYER_FILES = {
    "application/vnd.ms.bicep.module.layer.v1+json": "main.json",
    "application/vnd.ms.bicep.module.source.v1.tar+gzip": "source.tgz",
}


def sha256(data: bytes) -> str:
    """Return the lowercase artifact fingerprint."""
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Write a stable, reviewable JSON document."""
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def module_parts(reference: str) -> tuple[str, str]:
    """Accept only exact public resource-module pins."""
    match = REFERENCE.fullmatch(reference)
    if match is None:
        raise ValueError(f"expected a pinned br/public:avm/res reference: {reference!r}")
    return match[1], match[2]


def load_policy(path: Path) -> dict[str, Any]:
    """Validate the explicit selection and exclusions before reaching the registry."""
    policy = json.loads(path.read_bytes())
    if policy.get("schema") != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", policy["bicep_version"]):
        raise ValueError("unsupported Bicep dependency policy")
    modules = policy["modules"]
    if not modules or len(set(modules)) != len(modules):
        raise ValueError("module selection must be nonempty and unique")
    for reference in modules:
        module_parts(reference)
    for excluded in policy["excluded"]:
        if not excluded["selection"].strip() or not excluded["reason"].strip():
            raise ValueError("every exclusion needs a selection and reason")
    return policy


def lock(policy_path: Path, manifest_path: Path) -> None:
    """Resolve reviewed version pins to OCI manifest digests; never run during a build."""
    policy = load_policy(policy_path)
    modules = []
    for reference in policy["modules"]:
        module, version = module_parts(reference)
        request = urllib.request.Request(
            f"https://mcr.microsoft.com/v2/bicep/{module}/manifests/{version}",
            headers={"Accept": "application/vnd.oci.image.manifest.v1+json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        modules.append({"reference": reference, "manifest_sha256": sha256(data)})
    write_json(
        manifest_path,
        {
            "schema": 1,
            "bicep_version": policy["bicep_version"],
            "policy_sha256": sha256(policy_path.read_bytes()),
            "modules": modules,
            "excluded": policy["excluded"],
        },
    )


def verify_module(cache: Path, entry: dict[str, str]) -> dict[str, str]:
    """Verify the restored manifest and every executable/source layer against the lock."""
    module, version = module_parts(entry["reference"])
    directory = (
        cache / "br/mcr.microsoft.com" / ("bicep$" + module.replace("/", "$")) / (version + "$")
    )
    manifest_bytes = (directory / "manifest").read_bytes()
    if sha256(manifest_bytes) != entry["manifest_sha256"]:
        raise ValueError(f"registry manifest changed for {entry['reference']}")
    manifest = json.loads(manifest_bytes)
    found = set()
    for layer in manifest["layers"]:
        name = LAYER_FILES.get(layer["mediaType"])
        if name is None or name in found:
            raise ValueError("unsupported or duplicate Bicep module layer")
        found.add(name)
        data = (directory / name).read_bytes()
        if "sha256:" + sha256(data) != layer["digest"] or len(data) != layer["size"]:
            raise ValueError(f"restored layer does not match its manifest: {name}")
    if "main.json" not in found:
        raise ValueError("module has no compiled template layer")
    return {"reference": entry["reference"], "manifest_sha256": sha256(manifest_bytes)}


def prepare(policy_path: Path, manifest_path: Path, output: Path) -> None:
    """Restore into a fresh cache and write a receipt only after digest verification."""
    if output.exists():
        raise ValueError("preparation output must not exist")
    policy = load_policy(policy_path)
    manifest = json.loads(manifest_path.read_bytes())
    if (
        manifest.get("schema") != 1
        or manifest["policy_sha256"] != sha256(policy_path.read_bytes())
        or manifest["bicep_version"] != policy["bicep_version"]
        or manifest["excluded"] != policy["excluded"]
        or [entry["reference"] for entry in manifest["modules"]] != policy["modules"]
    ):
        raise ValueError("dependency manifest does not match the policy; regenerate and review it")
    version = subprocess.run(
        ["bicep", "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if not version.startswith(f"Bicep CLI version {policy['bicep_version']} "):
        raise ValueError("base image Bicep version does not match the policy")
    cache = output.resolve() / "cache"
    cache.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        write_json(work / "bicepconfig.json", {"cacheRootDirectory": str(cache)})
        (work / "main.bicep").write_text(
            "\n".join(
                f"module m{index} '{reference}' = {{name: 'm{index}'}}"
                for index, reference in enumerate(policy["modules"])
            ),
            encoding="utf-8",
        )
        subprocess.run(
            ["bicep", "restore", "main.bicep"],
            cwd=work,
            env={**os.environ, "HOME": temporary},
            check=True,
            timeout=600,
        )
    modules = [verify_module(cache, entry) for entry in manifest["modules"]]
    files = {}
    for path in sorted(cache.rglob("*")):
        if path.is_symlink():
            raise ValueError("cache must not contain symbolic links")
        if path.is_file():
            files[path.relative_to(cache).as_posix()] = sha256(path.read_bytes())
    write_json(
        output / "receipt.json",
        {
            "schema": 1,
            "bicep_version": version,
            "manifest_sha256": sha256(manifest_path.read_bytes()),
            "policy_sha256": sha256(policy_path.read_bytes()),
            "modules": modules,
            "files": files,
        },
    )


def verify_offline(output: Path) -> None:
    """Require every baked module to load its parameter schema without restoring."""
    receipt = json.loads((output / "receipt.json").read_bytes())
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        write_json(
            work / "bicepconfig.json",
            {
                "cacheRootDirectory": str(output.resolve() / "cache"),
                "analyzers": {"core": {"rules": {"use-recent-module-versions": {"level": "off"}}}},
            },
        )
        for entry in receipt["modules"]:
            (work / "main.bicep").write_text(
                f"module probe '{entry['reference']}' = {{name: 'probe'}}\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["bicep", "build", "main.bicep", "--no-restore", "--diagnostics-format", "sarif"],
                cwd=work,
                env={**os.environ, "HOME": temporary},
                capture_output=True,
                text=True,
                timeout=60,
            )
            report = json.loads(result.stderr)
            findings = [finding for run in report["runs"] for finding in run["results"]]
            # A probe omits required parameters; every other compiler error is unexpected.
            if (
                not report["runs"]
                or result.returncode not in (0, 1)
                or (result.returncode != 0 and not findings)
                or any(finding["ruleId"] != "BCP035" for finding in findings)
            ):
                raise ValueError(
                    f"offline module probe failed: {entry['reference']}: {result.stderr}"
                )


def main() -> None:
    """Lock a policy or prepare its verified artifacts for a Docker build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("lock", "prepare", "verify-offline"))
    parser.add_argument("--policy", type=Path, default=ROOT / "dependencies.bicep-avm.policy.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "dependencies.bicep-avm.json")
    parser.add_argument("--output", type=Path, default=Path("/prepared"))
    args = parser.parse_args()
    if args.action == "lock":
        lock(args.policy, args.manifest)
    elif args.action == "verify-offline":
        verify_offline(args.output)
    else:
        prepare(args.policy, args.manifest, args.output)


if __name__ == "__main__":
    main()
