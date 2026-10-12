"""Read the compiler identity shared by image builds and host verification."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def read_pin(path: Path) -> tuple[str, str]:
    """Require one stable version and SHA-256 without evaluating Dockerfile content."""
    text = path.read_text(encoding="utf-8")
    versions = re.findall(r"^ARG BICEP_VERSION=(.*)$", text, re.MULTILINE)
    digests = re.findall(r"^ARG BICEP_SHA256=(.*)$", text, re.MULTILINE)
    if (
        len(versions) != 1
        or len(digests) != 1
        or not re.fullmatch("v" + VERSION, versions[0])
        or not re.fullmatch(r"[0-9a-f]{64}", digests[0])
    ):
        raise ValueError("expected one stable Bicep version and SHA-256 pin")
    return versions[0], digests[0]


def main() -> None:
    """Print the validated version and digest as shell-safe words."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dockerfile", type=Path)
    args = parser.parse_args()
    print(*read_pin(args.dockerfile))


if __name__ == "__main__":
    main()
