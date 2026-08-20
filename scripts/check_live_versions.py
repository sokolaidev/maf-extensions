"""Assert a live sample ran against the package version the release just published.

Usage:
    python scripts/check_live_versions.py /tmp/sample-out.txt maf-sandbox 0.18.1
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_INSTALLED = re.compile(r"^\s*\[measured\] installed:\s*(.+)\s*$", re.MULTILINE)


def _parse_installed(output: str) -> tuple[tuple[str, str], ...]:
    """Every `(name, version)` pair the sample printed on its measured installed line."""
    match = _INSTALLED.search(output)
    if match is None:
        raise ValueError("the sample never printed a measured installed: line")
    seen: list[tuple[str, str]] = []
    for piece in match.group(1).split(","):
        text = piece.strip()
        if not text:
            continue
        name, sep, version = text.partition(" ")
        if not sep:
            raise ValueError(f"malformed installed pair: {text!r}")
        seen.append((name.strip(), version.strip()))
    return tuple(seen)


def assess(output: str, package: str, version: str) -> list[str]:
    """Every reason the sample output is not a healthy version report."""
    try:
        pairs = _parse_installed(output)
    except ValueError as error:
        return [str(error)]
    matches = [pair for pair in pairs if pair == (package, version)]
    if len(matches) == 1:
        return []
    return [
        f"expected exactly one installed match for {package} {version}, found: "
        f"{', '.join(f'{name} {ver}' for name, ver in pairs) or 'none'}"
    ]


def main(argv: list[str]) -> int:
    """Check that a sample reported the exact published package version it resolved."""
    if len(argv) != 4:
        print(f"usage: {argv[0]} <sample-output> <package> <version>", file=sys.stderr)
        return 2
    output = Path(argv[1]).read_text(encoding="utf-8")
    failures = assess(output, argv[2], argv[3])
    if failures:
        print("live version check failed:", file=sys.stderr)
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
