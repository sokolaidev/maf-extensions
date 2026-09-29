"""Run one PR compatibility shard, retaining the checker's local-core fallback."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def wheel_for(distribution: str) -> Path:
    """Require exactly one built wheel for a distribution."""
    wheels = sorted((ROOT / "dist").glob(f"{distribution.replace('-', '_')}-*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected one wheel for {distribution}, found {len(wheels)}")
    return wheels[0]


def main(argv: list[str]) -> int:
    """Partition discovered dependents round-robin and propagate checker failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, required=True, help="zero-based shard index")
    parser.add_argument("--shards", type=int, required=True, help="total number of shards")
    args = parser.parse_args(argv)
    if not 0 <= args.shard < args.shards:
        parser.error("require 0 <= --shard < --shards")

    packages = sorted(
        path
        for path in (ROOT / "packages").iterdir()
        if path.is_dir() and path.name != "maf-sandbox"
    )
    selected = packages[args.shard :: args.shards]
    if not selected:
        parser.error("shard contains no dependent packages")
    try:
        core = wheel_for("maf-sandbox")
        candidates = [(package.name, wheel_for(package.name)) for package in selected]
    except ValueError as error:
        parser.error(str(error))

    for distribution, wheel in candidates:
        print(f"Checking {distribution} (shard {args.shard + 1}/{args.shards})", flush=True)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "check_dependent_works_with_published_cores.py"),
                distribution,
                str(wheel),
                "--local-core",
                str(core),
            ],
            cwd=ROOT,
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
