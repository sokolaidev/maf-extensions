"""The local gate's package discovery — one implementation, shared with the CI guard.

`poe types-packages` calls `pyright_packages()` from here; `tests/test_pr_gate_enumerates.py`
imports the same function to pin that the *workflow* enumerates too. The rule both answer:
every `packages/*/` with its own `[tool.pyright]` gets its strict pass, so a new package is
covered on the commit that adds it (#450).
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def packages_with_pyright() -> list[Path]:
    """Every workspace package carrying its own strict pyright config."""
    found: list[Path] = []
    for config in sorted((REPO_ROOT / "packages").glob("*/pyproject.toml")):
        table = tomllib.loads(config.read_text(encoding="utf-8"))
        if "pyright" in table.get("tool", {}):
            found.append(config.parent)
    return found


def pyright_packages() -> int:
    """Run each package's strict pyright pass; the process exit code is the answer."""
    failures: list[str] = []
    for package in packages_with_pyright():
        print(f"pyright: {package.name}")
        result = subprocess.run(
            [sys.executable, "-m", "pyright", "-p", str(package)],
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode != 0:
            failures.append(package.name)
    if failures:
        print(f"pyright failed for: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0
