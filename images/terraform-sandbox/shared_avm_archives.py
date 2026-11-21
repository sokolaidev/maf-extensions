"""Fetch the commit archives pinned by both release catalogues into a build-only layer."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import terraform_dependencies as prep  # noqa: E402


def download(terraform: Path, opentofu: Path, output: Path) -> None:
    """Export common archives, keyed by their verified content digest."""

    def archives(path: Path) -> dict[tuple[str, str], dict[str, str]]:
        packages = prep.load_manifest(path.read_bytes())["registry_modules"]
        return {
            (item["artifact"]["url"].lower(), item["artifact"]["sha256"]): item["artifact"]
            for item in packages
        }

    first, second = archives(terraform), archives(opentofu)
    output.mkdir()
    deadline = time.monotonic() + prep.DEADLINE
    remaining = prep.MAX_TOTAL
    for key in sorted(first.keys() & second.keys()):
        data = prep.fetch(first[key], deadline, max_bytes=remaining)
        remaining -= len(data)
        (output / key[1]).write_bytes(data)


if __name__ == "__main__":
    download(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
