"""Verify the PNG and editable file retained by the Docker draw.io sample."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from check_live_diagram_sample import dimensions
from live_sample_evidence import duration, records


def verify(log: str, output: Path) -> None:
    """Require one clean call and match its measurements to the retained files."""
    measurements = records(log)
    if len(measurements) != 1:
        raise ValueError("Expected one draw.io export measurement")
    [render] = measurements
    if (
        render.get("tool") != "create_drawio"
        or not duration(render.get("seconds"))
        or type(render.get("unclean")) is not int
        or render["unclean"] != 0
        or render.get("undisposed") is not False
    ):
        raise ValueError("Draw.io export or sandbox cleanup was incomplete")

    drawio = (output / "diagram.drawio").read_bytes()
    png = (output / "diagram-1.png").read_bytes()
    size = dimensions(png)
    if (
        not drawio
        or size is None
        or min(size) <= 0
        or size
        != (
            render.get("width"),
            render.get("height"),
        )
    ):
        raise ValueError("Retained draw.io or PNG output is invalid")
    if hashlib.sha256(drawio).hexdigest() != render.get("drawio_sha256") or hashlib.sha256(
        png
    ).hexdigest() != render.get("png_sha256"):
        raise ValueError("Retained files differ from the measured export")


def main() -> None:
    """Check the live log and files from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        verify(args.log.read_text(encoding="utf-8"), args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Draw.io PNG verification failed: {error}\n")
    print("Verified the editable diagram, PNG and sandbox cleanup")


if __name__ == "__main__":
    main()
