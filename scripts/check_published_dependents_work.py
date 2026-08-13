"""Refuse to publish a maf-sandbox the already-published dependents can no longer import.

    python scripts/check_published_dependents_work.py <version> <core-wheel>

The admit check (`check_published_dependents_admit.py`) asks only whether each dependent's
ceiling permits the version going out; it never runs the dependent, so a core that removed a name
a dependent imports passes that check and ships. This is the missing half: install the candidate
core wheel alongside each published dependent that admits it and confirm the dependent still
imports. A break is the signal — no changelog inference.

Only the LATEST published version of each dependent is tested; old published versions that still
admit the candidate but break are not caught. A dependent whose ceiling excludes the version is
the admit check's concern and is skipped here, and one not yet on PyPI is skipped too. A network
failure is fatal rather than skipped: passing because PyPI could not be reached is the one outcome
that would make this check worthless — the same stance as the admit check.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from check_published_dependents_admit import (
    ceiling_of,
    dependent_distributions,
    fetch_requires_dist,
)
from check_release_order import admits, version


def import_module(distribution: str) -> str:
    """The importable module name for a distribution: ``maf-sandbox-bicep`` -> ``maf_sandbox_bicep``."""
    return distribution.replace("-", "_")


def at_risk(
    published: dict[str, list[str] | None], released: tuple[int, ...]
) -> list[str]:
    """Published dependents whose maf-sandbox ceiling admits ``released``, sorted by name.

    The inverse of ``check_published_dependents_admit.refusals``: a dependent is at-risk when its
    ceiling is None (unbounded) or admits the version. A dependent not yet on PyPI (None) is
    absent — it has nothing to contradict. A dependent whose ceiling excludes the version is the
    admit check's domain and is absent here.
    """
    found: list[str] = []
    for distribution, requires_dist in sorted(published.items()):
        if requires_dist is None:
            continue
        ceiling = ceiling_of(requires_dist)
        if ceiling is None or admits(released, ceiling):
            found.append(distribution)
    return found


def breaks(
    core_wheel: Path,
    candidates: list[str],
    install_and_import: Callable[[Path, str], str | None],
) -> list[str]:
    """One line per candidate that no longer imports against ``core_wheel``.

    ``install_and_import`` is the one impure step — a clean venv, the local core wheel plus the
    dependent from PyPI, then ``import`` — and it is passed in so this decision is testable with a
    fake. A None return is a pass; a one-line reason is a failure.
    """
    failed: list[str] = []
    for distribution in candidates:
        error = install_and_import(core_wheel, distribution)
        if error is not None:
            failed.append(f"{distribution}: {error}")
    return failed


def _venv_python(venv: Path) -> Path:
    """The interpreter uv creates inside a venv, on either platform.

    ``uv venv`` lays down ``bin/`` on POSIX and ``Scripts/`` on Windows, and the manual run that
    proves this check is as likely to happen on one as the other.
    """
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def install_and_import(core_wheel: Path, distribution: str) -> str | None:
    """Install the core wheel and the latest dependent into a throwaway venv, then import it.

    The local wheel is what gets imported: it and the dependent are resolved in one
    ``uv pip install``, so the explicit file requirement pins ``maf-sandbox`` to the candidate
    version and uv does not re-fetch the core from PyPI. Returns None on a clean import, or a
    one-line reason on a resolution or import failure.
    """
    module = import_module(distribution)
    with tempfile.TemporaryDirectory() as root:
        venv = Path(root) / "venv"
        python = _venv_python(venv)
        create = subprocess.run(
            ["uv", "venv", str(venv)], capture_output=True, text=True
        )
        if create.returncode != 0:
            return f"uv venv failed: {create.stderr.strip() or create.stdout.strip()}"
        install = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                str(core_wheel),
                distribution,
            ],
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            return f"install failed: {install.stderr.strip() or install.stdout.strip()}"
        probe = subprocess.run(
            [str(python), "-c", f"import {module}"], capture_output=True, text=True
        )
        if probe.returncode != 0:
            lines = (probe.stderr or probe.stdout).strip().splitlines()
            return lines[-1].strip() if lines else f"import {module} failed"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <version> <core-wheel>", file=sys.stderr)
        return 2
    released = version(argv[1])
    core_wheel = Path(argv[2])
    if not core_wheel.is_file():
        print(f"no core wheel at {core_wheel}", file=sys.stderr)
        return 1
    repo_root = Path(__file__).resolve().parent.parent
    published = {
        distribution: fetch_requires_dist(distribution)
        for distribution in dependent_distributions(repo_root)
    }
    candidates = at_risk(published, released)
    if not candidates:
        print(f"no published dependent admits maf-sandbox {argv[1]}; nothing to verify")
        return 0
    failures = breaks(core_wheel, candidates, install_and_import)
    if not failures:
        names = ", ".join(candidates)
        print(
            f"every published dependent that admits maf-sandbox {argv[1]} imports against it "
            f"({names})"
        )
        return 0
    for failure in failures:
        print(failure, file=sys.stderr)
    print(
        f"\nPublishing maf-sandbox {argv[1]} breaks the dependents above at import time. "
        "Release a core that keeps them importing, or widen and re-release the dependents first: "
        "RELEASING.md, Release order.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
