"""The local gate's package discovery: every `packages/*/` with a `[tool.pyright]` section."""

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


def main(argv: list[str]) -> int:
    """The CLI CI calls: one command per gate task, exit code as the verdict."""
    if argv == ["pyright-packages"]:
        return pyright_packages()
    print(f"usage: {Path(sys.argv[0]).name} pyright-packages", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
