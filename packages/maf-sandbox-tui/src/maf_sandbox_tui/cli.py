"""Command-line entry point for MST."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from ._app import SandboxConsole
from ._client import HttpControl, discover_controls
from ._control import MemoryControl, SandboxControl
from ._server import EndpointManifest, SandboxControlServer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mst",
        description="Inspect and dispose sandboxes owned by opted-in MAF applications.",
    )
    parser.add_argument("--demo", action="store_true", help="run against a temporary demo host")
    parser.add_argument("--json", action="store_true", help="print one inventory snapshot and exit")
    parser.add_argument("--endpoint", help="connect directly instead of discovering local hosts")
    parser.add_argument("--token", help="bearer token for --endpoint")
    parser.add_argument("--source", default="manual", help="label for --endpoint")
    return parser


async def _show(control: SandboxControl, *, as_json: bool) -> None:
    if as_json:
        records = await control.list_sandboxes()
        print(json.dumps([record.to_json() for record in records], indent=2))
        return
    await SandboxConsole(control).run_async()


async def _run(arguments: argparse.Namespace) -> None:
    if arguments.endpoint:
        if not arguments.token:
            raise SystemExit("--token is required with --endpoint")
        manifest = EndpointManifest(
            arguments.source,
            arguments.endpoint.rstrip("/"),
            arguments.token,
            0,
        )
        await _show(HttpControl(manifest), as_json=arguments.json)
        return
    if arguments.token:
        raise SystemExit("--token requires --endpoint")
    if arguments.demo:
        with tempfile.TemporaryDirectory(prefix="mst-demo-") as temporary:
            control = MemoryControl.demo()
            async with SandboxControlServer(
                control,
                source_id="mst-demo",
                manifest_directory=Path(temporary),
            ) as server:
                await _show(HttpControl(server.manifest), as_json=arguments.json)
        return
    await _show(await discover_controls(), as_json=arguments.json)


def main(argv: Sequence[str] | None = None) -> None:
    """Run MST using local discovery, a direct endpoint, or its demonstration host."""
    arguments = _parser().parse_args(argv)
    if os.environ.get("NO_COLOR") is not None:
        os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "standard")
    asyncio.run(_run(arguments))
