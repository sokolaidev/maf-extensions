"""Fetch Go build dependencies with bounded retries before verification and testing."""

import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path


def download_modules(checkout: Path, modules: Mapping[str, str]) -> None:
    """Apply pinned module updates and populate the cache, with three attempts per command."""
    commands = (
        ["go", "get", *(name + "@" + version for name, version in modules.items())],
        ["go", "mod", "download"],
    )
    for command in commands:
        for attempt in range(3):
            try:
                subprocess.run(command, cwd=checkout, check=True)
                break
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
                delay = 2 ** (attempt + 1)
                print(
                    f"{' '.join(command)} failed; retrying ({attempt + 2}/3) in {delay}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
