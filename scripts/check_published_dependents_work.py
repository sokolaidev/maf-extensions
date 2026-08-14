"""Refuse to publish a maf-sandbox the already-published dependents can no longer import.

    python scripts/check_published_dependents_work.py <version> <core-wheel> [--latest-only]

The admit check (`check_published_dependents_admit.py`) asks only whether each dependent's
ceiling permits the version going out; it never runs the dependent, so a core that removed a name
a dependent imports passes that check and ships. This is the missing half: install the candidate
core wheel alongside each published dependent that admits it and confirm the dependent still
imports. A break is the signal — no changelog inference.

By default every published version of each dependent whose ceiling admits the candidate is
tested, not just the latest. A dependent's old releases sit on PyPI with the ceilings they
shipped with, and an old version with a loose ceiling can admit a breaking core the latest has
already moved off — exactly the case latest-only misses. ``--latest-only`` restricts the test to
the latest version per dependent: the fast re-check used at upload time, with reduced coverage
rather than a complete re-run. Old versions are immutable and were tested at build time, so the
common new risk during the approval wait is a newly published latest; narrower races — a yanked
version unyanked in the window, or a newly uploaded non-latest version — are not caught by it,
and closing them means re-running every version at upload, which the build/upload split exists to
avoid. A dependent whose ceiling excludes the version is the admit check's concern and is skipped
here, and one not yet on PyPI is skipped too. A network failure is fatal rather than skipped:
passing because PyPI could not be reached is the one outcome that would make this check
worthless — the same stance as the admit check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from check_published_dependents_admit import ceiling_of, dependent_distributions
from check_release_order import admits, version

_TIMEOUT_SECONDS = 30


def import_module(distribution: str) -> str:
    """The importable module name for a distribution: ``maf-sandbox-bicep`` -> ``maf_sandbox_bicep``."""
    return distribution.replace("-", "_")


def _fetch_project(distribution: str) -> dict[str, Any] | None:
    """The top-level PyPI project JSON, or None if the distribution was never released."""
    url = f"https://pypi.org/pypi/{distribution}/json"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def fetch_latest(distribution: str) -> tuple[str, list[str]] | None:
    """The latest published version and its ``requires_dist``, or None if never released.

    One top-level fetch reuses the index's ``info`` block, which already carries the latest
    version and its requirements. A yanked latest is skipped because a real user never lands on
    one: normal unpinned resolution ignores yanked releases, so a break there is not a real-user
    break. The ``==`` pin this script uses would still install a yanked release (PEP 592), so
    skipping is what aligns the test with what users actually run.
    """
    payload = _fetch_project(distribution)
    if payload is None:
        return None
    info = payload["info"]
    if info.get("yanked"):
        return None
    return info["version"], list(info.get("requires_dist") or [])


def fetch_requires_dist_for_version(
    distribution: str, version_str: str
) -> list[str] | None:
    """One version's ``requires_dist``, or None if that version is gone or yanked.

    The top-level index JSON carries ``requires_dist`` only for the latest version; every other
    version needs its own per-version fetch. A yanked version is skipped — not because the ``==``
    pin cannot install it (PEP 592 allows that, with a warning) but because normal unpinned
    resolution never selects a yanked release, so a user does not land on one and a break there
    is not a real-user break.
    """
    url = f"https://pypi.org/pypi/{distribution}/{version_str}/json"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    info = payload["info"]
    if info.get("yanked"):
        return None
    return list(info.get("requires_dist") or [])


def fetch_version_requirements(distribution: str) -> dict[str, list[str]] | None:
    """Every non-yanked published version's ``requires_dist``, or None if never released.

    The latest version reuses the top-level ``info.requires_dist`` (one fetch already made); every
    other version needs its own fetch. A per-version 404 (an empty or pulled release) is skipped
    rather than fatal — the version is simply not testable, not a break.
    """
    payload = _fetch_project(distribution)
    if payload is None:
        return None
    info = payload["info"]
    latest_version = info["version"]
    by_version: dict[str, list[str]] = {}
    if not info.get("yanked"):
        by_version[latest_version] = list(info.get("requires_dist") or [])
    for version_str in payload["releases"]:
        if version_str == latest_version:
            continue
        requires = fetch_requires_dist_for_version(distribution, version_str)
        if requires is not None:
            by_version[version_str] = requires
    return by_version


def at_risk(
    published: dict[str, list[tuple[str, list[str]]] | None],
    released: tuple[int, ...],
) -> list[tuple[str, str]]:
    """Published ``(distribution, version)`` pairs whose maf-sandbox ceiling admits ``released``.

    The inverse of ``check_published_dependents_admit.refusals``: a version is at-risk when its
    ceiling is None (unbounded) or admits the released version. A dependent not yet on PyPI
    (None) is absent — it has nothing to contradict. A version whose ceiling excludes the release
    is the admit check's domain and is absent here. Pairs are sorted by distribution name then by
    version, so the output is stable regardless of fetch order.
    """
    found: list[tuple[str, str]] = []
    for distribution, versions in sorted(published.items()):
        if versions is None:
            continue
        for version_str, requires_dist in sorted(
            versions, key=lambda vr: version(vr[0])
        ):
            ceiling = ceiling_of(requires_dist)
            if ceiling is None or admits(released, ceiling):
                found.append((distribution, version_str))
    return found


def breaks(
    core_wheel: Path,
    candidates: list[tuple[str, str]],
    install_and_import: Callable[[Path, str, str], str | None],
) -> list[str]:
    """One line per ``(distribution, version)`` that no longer imports against ``core_wheel``.

    ``install_and_import`` is the one impure step — a clean venv, the local core wheel plus the
    pinned dependent from PyPI, then ``import`` — and it is passed in so this decision is testable
    with a fake. A None return is a pass; a one-line reason is a failure.
    """
    failed: list[str] = []
    for distribution, version_str in candidates:
        error = install_and_import(core_wheel, distribution, version_str)
        if error is not None:
            failed.append(f"{distribution}=={version_str}: {error}")
    return failed


def _venv_python(venv: Path) -> Path:
    """The interpreter uv creates inside a venv, on either platform.

    ``uv venv`` lays down ``bin/`` on POSIX and ``Scripts/`` on Windows, and the manual run that
    proves this check is as likely to happen on one as the other.
    """
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def install_and_import(
    core_wheel: Path, distribution: str, version_str: str
) -> str | None:
    """Install the core wheel and the pinned dependent into a throwaway venv, then import it.

    The local wheel is what gets imported: it and the dependent are resolved in one
    ``uv pip install``, so the explicit file requirement pins ``maf-sandbox`` to the candidate
    version and uv does not re-fetch the core from PyPI. The dependent is pinned to
    ``version_str`` so the exact published release — not whatever uv would otherwise resolve —
    is the one tested. Returns None on a clean import, or a one-line reason on a resolution or
    import failure.
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
                f"{distribution}=={version_str}",
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


def _latest_versions(
    latest: tuple[str, list[str]] | None,
) -> list[tuple[str, list[str]]] | None:
    """The latest-only shape: one ``(version, requires_dist)`` pair, or None if unpublished."""
    if latest is None:
        return None
    version_str, requires_dist = latest
    return [(version_str, requires_dist)]


def _all_versions(
    by_version: dict[str, list[str]] | None,
) -> list[tuple[str, list[str]]] | None:
    """The all-versions shape: every ``(version, requires_dist)`` pair, or None if unpublished."""
    if by_version is None:
        return None
    return list(by_version.items())


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    latest_only = False
    if args and args[-1] == "--latest-only":
        latest_only = True
        args = args[:-1]
    if len(args) != 2:
        print(
            f"usage: {argv[0]} <version> <core-wheel> [--latest-only]", file=sys.stderr
        )
        return 2
    released = version(args[0])
    core_wheel = Path(args[1])
    if not core_wheel.is_file():
        print(f"no core wheel at {core_wheel}", file=sys.stderr)
        return 1
    repo_root = Path(__file__).resolve().parent.parent
    distributions = dependent_distributions(repo_root)
    if latest_only:
        published = {
            distribution: _latest_versions(fetch_latest(distribution))
            for distribution in distributions
        }
    else:
        published = {
            distribution: _all_versions(fetch_version_requirements(distribution))
            for distribution in distributions
        }
    candidates = at_risk(published, released)
    if not candidates:
        print(f"no published dependent admits maf-sandbox {args[0]}; nothing to verify")
        return 0
    failures = breaks(core_wheel, candidates, install_and_import)
    if not failures:
        names = ", ".join(f"{d}=={v}" for d, v in candidates)
        print(
            f"every published dependent that admits maf-sandbox {args[0]} imports against it "
            f"({names})"
        )
        return 0
    for failure in failures:
        print(failure, file=sys.stderr)
    print(
        f"\nPublishing maf-sandbox {args[0]} breaks the dependents above at import time. "
        "Release a core that keeps them importing, or widen and re-release the dependents first: "
        "RELEASING.md, Release order.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
