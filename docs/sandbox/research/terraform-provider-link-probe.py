"""Measure Terraform provider installation from an unpacked filesystem mirror, inside a guest.

    python3 -I terraform-provider-link-probe.py --mirror DIR [--unpack-from DIR] [--read-first FILE]

Standard library only. Each provider-only root is initialized and validated with a fresh private
data directory. It reports time, bytes written as regular files, symlinks and chosen versions.
`--unpack-from` turns a packed mirror into the unpacked layout first; that setup is not measured.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

ROOTS = {
    "typical": {
        "hashicorp/azurerm": "~> 4.0",
        "azure/azapi": "~> 2.4",
        "azure/modtm": "~> 0.3",
        "hashicorp/random": "~> 3.5",
    },
    "legacy": {"hashicorp/azurerm": "~> 3.116", "hashicorp/azuread": "~> 2.47"},
    "newest": {
        "hashicorp/azurerm": ">= 5.0",
        "hashicorp/azuread": "~> 3.0",
        "hashicorp/time": "~> 0.13",
    },
    "azapi-only": {"azure/azapi": "~> 2.4"},
    # A constraint the mirror cannot satisfy must fail initialization, not pick another version.
    "unsatisfiable": {"hashicorp/azurerm": "~> 6.0"},
    "every-provider": {
        "hashicorp/azurerm": ">= 5.0",
        "azure/azapi": "~> 2.4",
        "hashicorp/azuread": "~> 3.0",
        "azure/modtm": "~> 0.4",
        "hashicorp/time": "~> 0.14",
        "azure/alz": "~> 0.22",
        "microsoft/azuredevops": "~> 1.0",
        "integrations/github": "~> 6.0",
        "chilicat/pkcs12": "~> 0.0.7",
        "lonegunmanb/ephemeraltls": "~> 0.2",
        "hashicorp/assert": "~> 0.15",
        "hashicorp/local": "~> 2.0",
        "hashicorp/null": "~> 3.0",
        "hashicorp/random": "~> 3.5",
        "hashicorp/tls": "~> 4.0",
    },
}
PACKED = re.compile(r"terraform-provider-([a-z0-9-]+)_([0-9.]+)_linux_amd64\.zip")


def unpack(packed: Path, mirror: Path) -> None:
    """Convert HOST/NAMESPACE/TYPE/<zip> into HOST/NAMESPACE/TYPE/VERSION/linux_amd64/."""
    for archive in packed.rglob("*.zip"):
        match = PACKED.fullmatch(archive.name)
        if match is None:
            continue
        target = mirror / archive.parent.relative_to(packed) / match.group(2) / "linux_amd64"
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(target)
        for path in target.iterdir():
            path.chmod(0o755)


def available(mirror: Path) -> set[str]:
    """Provider addresses present in the unpacked mirror."""
    host = mirror / "registry.terraform.io"
    return {f"{ns.name}/{kind.name}" for ns in host.iterdir() for kind in ns.iterdir()}


def written(data: Path, mirror: Path) -> dict[str, int]:
    """Count regular-file bytes and symlinks under the data directory, without following links."""
    regular = links = foreign = 0
    for directory, dirs, files in os.walk(data):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                links += 1
                foreign += not os.readlink(path).startswith(str(mirror))
            elif path.is_file():
                regular += path.stat().st_size
    return {"regular_bytes": regular, "symlinks": links, "symlinks_outside_mirror": foreign}


def drop_caches() -> bool:
    """Drop the page cache when the guest allows it; report whether it did."""
    try:
        subprocess.run(["sync"], check=False)
        Path("/proc/sys/vm/drop_caches").write_text("3")
        return True
    except OSError:
        return False


def memory() -> dict[str, int | None]:
    """Read guest memory totals and, where cgroup v2 exposes it, the peak."""
    info = dict(
        line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line
    )
    peak = Path("/sys/fs/cgroup/memory.peak")
    return {
        "mem_total_kib": int(info["MemTotal"].split()[0]),
        "mem_available_kib": int(info["MemAvailable"].split()[0]),
        "cgroup_peak_bytes": int(peak.read_text()) if peak.exists() else None,
    }


def run(root: Path, config: Path, mirror: Path, lockfile: bool) -> dict[str, object]:
    """Initialize and validate one root with a fresh data directory."""
    data = Path(tempfile.mkdtemp(prefix="data-", dir=root.parent))
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(root.parent),
        "TF_DATA_DIR": str(data),
        "TF_CLI_CONFIG_FILE": str(config),
        "TF_IN_AUTOMATION": "1",
        "CHECKPOINT_DISABLE": "1",
    }
    command = ["terraform", "init", "-backend=false", "-input=false", "-no-color"]
    if lockfile:
        command.append("-lockfile=readonly")
    else:
        (root / ".terraform.lock.hcl").unlink(missing_ok=True)
    start = time.monotonic()
    init = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
    middle = time.monotonic()
    validate = subprocess.run(
        ["terraform", "validate", "-json"], cwd=root, env=env, capture_output=True, text=True
    )
    end = time.monotonic()
    lock = (
        (root / ".terraform.lock.hcl").read_text()
        if (root / ".terraform.lock.hcl").exists()
        else ""
    )
    result = {
        "init_exit": init.returncode,
        "init_seconds": round(middle - start, 2),
        "validate_valid": json.loads(validate.stdout).get("valid")
        if validate.stdout.startswith("{")
        else None,
        "validate_seconds": round(end - middle, 2),
        "versions": dict(
            re.findall(
                r'provider "registry\.terraform\.io/([^"]+)" \{\s*version\s*=\s*"([^"]+)"', lock
            )
        ),
        **written(data, mirror),
    }
    if init.returncode:
        result["init_error"] = init.stdout[-600:]
    shutil.rmtree(data)
    return result


def main() -> None:
    """Run every selected root whose providers the mirror holds and print the evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--unpack-from", type=Path)
    parser.add_argument("--read-first", type=Path)
    parser.add_argument("--roots", default=",".join(ROOTS))
    args = parser.parse_args()
    evidence: dict[str, object] = {"nproc": os.cpu_count(), "memory_start": memory()}
    if args.read_first:
        start = time.monotonic()
        with args.read_first.open("rb") as stream:
            size = sum(len(chunk) for chunk in iter(lambda: stream.read(1 << 20), b""))
        seconds = time.monotonic() - start
        evidence["first_read"] = {"bytes": size, "seconds": round(seconds, 3)}
    if args.unpack_from:
        unpack(args.unpack_from, args.mirror)
    mirror = args.mirror.resolve()
    evidence["mirror_bytes"] = sum(p.stat().st_size for p in mirror.rglob("*") if p.is_file())
    work = Path(tempfile.mkdtemp(prefix="provider-links-"))
    config = work / "terraform.rc"
    config.write_text(
        f'provider_installation {{\n  filesystem_mirror {{ path = "{mirror}" }}\n}}\n'
        'host "registry.terraform.io" {\n  services = {}\n}\n'
    )
    present = available(mirror)
    evidence["cache_drop_permitted"] = drop_caches()
    results = {}
    for name, providers in ROOTS.items():
        if name not in args.roots.split(",") or not set(providers) <= present:
            continue
        root = work / name
        root.mkdir()
        body = "".join(
            f'    {address.split("/")[1]} = {{ source = "{address}", version = "{constraint}" }}\n'
            for address, constraint in providers.items()
        )
        (root / "main.tf").write_text(f"terraform {{\n  required_providers {{\n{body}  }}\n}}\n")
        runs = {}
        if evidence["cache_drop_permitted"]:
            drop_caches()
            runs["cold"] = run(root, config, mirror, lockfile=False)
        runs["warm"] = run(root, config, mirror, lockfile=False)
        runs["readonly_lock"] = run(root, config, mirror, lockfile=True)
        if evidence["cache_drop_permitted"]:
            drop_caches()
            runs["readonly_lock_cold"] = run(root, config, mirror, lockfile=True)
        results[name] = runs
    evidence["roots"] = results
    evidence["memory_end"] = memory()
    print(json.dumps(evidence, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
