"""Run offline Draw.io export checks without an engine, using a provisioned Linux runtime."""

import argparse
import asyncio
from pathlib import Path

from check_drawio_exports import check
from maf_sandbox_bubblewrap import BubblewrapSandboxBackend, BubblewrapSandboxConfig


async def main() -> None:
    """Probe the host boundary and exercise native PNG, JPG and SVG exports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backend = await BubblewrapSandboxBackend.create(
        BubblewrapSandboxConfig(
            runtime_root=args.runtime,
            state_root=args.state,
            cgroup_root=args.cgroup,
        )
    )
    await check("native", args.output, backend)


if __name__ == "__main__":
    asyncio.run(main())
