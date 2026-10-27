"""Prepare verified provider packages for Docker builds using an optional persistent cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "images/terraform-sandbox"))
import build_providers as builder  # noqa: E402

MANIFESTS = {
    "terraform-random": "dependencies.terraform.json",
    "opentofu-random": "dependencies.opentofu.json",
    "terraform-avm": "dependencies.terraform-avm.json",
    "opentofu-avm": "dependencies.opentofu-avm-network.json",
}


def plan(manifest: Path) -> dict[str, str]:
    """Snapshot keys differ by provider set; individual package keys remain profile-independent."""
    document = builder.prep.load_manifest(manifest.read_bytes())
    keys = sorted(builder.cache_key(p) for p in document["providers"] if "source_build" in p)
    return {
        "enabled": str(bool(keys)).lower(),
        "key": hashlib.sha256(json.dumps(keys).encode()).hexdigest(),
        "manifest": manifest.resolve().relative_to(ROOT).as_posix(),
    }


def prepare(manifest: Path, cache: Path, output: Path) -> dict[str, Any]:
    """Build in the pinned Linux toolchain and export packages for the existing preparation gate."""
    started = time.monotonic()
    relative = manifest.resolve().relative_to(ROOT).as_posix()
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    image = "maf-provider-cache:toolchain"
    subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--file",
            "images/terraform-sandbox/prepared.Dockerfile",
            "--target",
            "provider-toolchain",
            "--tag",
            image,
            "images/terraform-sandbox",
        ],
        cwd=ROOT,
        check=True,
        timeout=600,
    )
    name = "maf-provider-build-" + uuid.uuid4().hex
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--platform",
        "linux/amd64",
        "--mount",
        f"type=bind,src={ROOT},dst=/src,readonly",
        "--mount",
        f"type=bind,src={cache.resolve()},dst=/cache",
        "--mount",
        f"type=bind,src={output.resolve()},dst=/out",
        image,
        "python3",
        "/src/images/terraform-sandbox/build_providers.py",
        "--manifest",
        f"/src/{relative}",
        "--output",
        "/out/builds",
        "--cache-directory",
        "/cache",
        "--report",
        "/out/providers.jsonl",
    ]
    try:
        subprocess.run(command, cwd=ROOT, check=True, timeout=7200)
    except BaseException:
        subprocess.run(
            ["docker", "rm", "--force", name], check=False, timeout=30, capture_output=True
        )
        raise
    records = [json.loads(line) for line in (output / "providers.jsonl").read_text().splitlines()]
    summary = {
        "manifest": relative,
        "providers": records,
        "total_seconds": time.monotonic() - started,
        "cache_bytes": sum(p.stat().st_size for p in cache.glob("*.zip") if not p.is_symlink()),
        "hits": sum(r["cache"] == "hit" for r in records),
        "builds": sum(r["cache"] != "hit" for r in records),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as report:
            report.write(
                f"\nProvider preparation: {summary['hits']} verified hits, "
                f"{summary['builds']} builds; "
                f"{summary['total_seconds']:.1f}s including toolchain setup; "
                f"{summary['cache_bytes']} cached bytes.\n"
            )
    return summary


def benchmark_manifest(engine: str, output: Path) -> None:
    """Use the same reviewed random provider in two small, distinct engine profiles."""
    original = json.loads(
        (ROOT / "images/terraform-sandbox/dependencies.terraform-avm.json").read_text()
    )
    providers = [p for p in original["providers"] if p["source"].endswith("/random")]
    if engine == "opentofu":
        providers.extend(p for p in original["providers"] if p["source"].endswith("/null"))
    if len(providers) != (1 if engine == "terraform" else 2):
        raise ValueError("Benchmark requires one reviewed version of each selected provider")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"schema": 1, "engine": engine, "providers": providers, "modules": []}) + "\n",
        encoding="utf-8",
    )


def verify_export(manifest: Path, output: Path) -> None:
    """Exercise the production Docker preparation stage offline with the exported packages."""
    context = output / "context"
    context.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest, context / "manifest.json")
    shutil.copyfile(ROOT / "images/terraform-sandbox/runner.py", context / "runner.py")
    subprocess.run(
        [
            "docker",
            "build",
            "--network",
            "none",
            "--platform",
            "linux/amd64",
            "--build-context",
            f"scripts={ROOT / 'scripts'}",
            "--build-context",
            f"provider-build={output.resolve()}",
            "--build-arg",
            "BASE_IMAGE=maf-provider-cache:toolchain",
            "--build-arg",
            "MANIFEST=manifest.json",
            "--file",
            str(ROOT / "images/terraform-sandbox/prepared.Dockerfile"),
            "--target",
            "prepared",
            "--output",
            f"type=local,dest={output / 'prepared'}",
            str(context),
        ],
        check=True,
        timeout=300,
    )
    receipt = json.loads((output / "prepared/receipt.json").read_text())
    expected = builder.prep.load_manifest(manifest.read_bytes())["providers"]
    actual = {(p["source"], p["version"]): p["sha256"] for p in receipt["providers"]}
    if actual != {(p["source"], p["version"]): p["source_build"]["sha256"] for p in expected}:
        raise ValueError("Docker preparation did not retain the verified provider identities")


def main() -> None:
    """Plan cache snapshots, prepare packages, or select bounded benchmark profiles."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "prepare", "benchmark", "verify"))
    parser.add_argument("--profile", choices=MANIFESTS)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--cache-directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--engine", choices=("terraform", "opentofu"))
    args = parser.parse_args()
    if args.operation == "benchmark":
        if args.engine is None or args.output is None:
            parser.error("benchmark requires --engine and --output")
        benchmark_manifest(args.engine, args.output)
        return
    manifest = args.manifest
    if manifest is None:
        if args.profile is None:
            parser.error("provide --profile or --manifest")
        manifest = ROOT / "images/terraform-sandbox" / MANIFESTS[args.profile]
    if args.operation == "plan":
        for name, value in plan(manifest).items():
            print(f"{name}={value}")
    elif args.operation == "verify":
        if args.output is None:
            parser.error("verify requires --output")
        verify_export(manifest, args.output)
    else:
        if args.cache_directory is None or args.output is None:
            parser.error("prepare requires --cache-directory and --output")
        print(json.dumps(prepare(manifest, args.cache_directory, args.output), indent=2))


if __name__ == "__main__":
    main()
