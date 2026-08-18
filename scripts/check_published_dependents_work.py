"""Refuse to publish a maf-sandbox the already-published dependents can no longer import.

    python scripts/check_published_dependents_work.py <version> <core-wheel> \
        [--emit-snapshot <path> | --since-snapshot <path>] [--dispatch]

The admit check (`check_published_dependents_admit.py`) asks only whether each dependent's
ceiling permits the version going out; it never runs the dependent, so a core that removed a name
a dependent imports passes that check and ships. This is the missing half: install the candidate
core wheel alongside each published dependent that admits it and confirm the dependent still
imports. A break is the signal — no changelog inference.

Every published version of each dependent whose ceiling admits the candidate is tested, not just
the latest. A dependent's old releases sit on PyPI with the ceilings they shipped with, and an old
version with a loose ceiling can admit a breaking core the latest has already moved off. The build
job runs this thorough pass and records the ``(distribution, version)`` pairs it tested with
``--emit-snapshot``; the upload job loads that snapshot with ``--since-snapshot`` and re-tests only
the versions admitting now that were not in it. A published wheel is immutable, so a version the
build already tested imports the same at upload and needs no re-run; the only new risk in the
approval window is a version that was not testable at build — one uploaded in the window, or a
yanked version unyanked in it — and the diff catches exactly those. The common case, where nothing
new appeared, installs nothing. A dependent whose ceiling excludes the version is the admit
check's concern and is skipped here, and one not yet on PyPI is skipped too. A network failure is
fatal rather than skipped: passing because PyPI could not be reached is the one outcome that would
make this check worthless — the same stance as the admit check.

Both runs print a ``live_check=`` verdict on stdout: ``live_check=run`` when at least one
admitting dependent was tested and every one imported, and ``live_check=skip`` when none admits the
candidate yet — the window where a live run would go red for the ordering of the release train
rather than for the code ([#273]). The build run's verdict is provisional — early validation that
refuses the release on a break before a human is asked to approve. The dispatch decision is the
*post-upload* re-check's verdict (``--dispatch``): the upload itself is a window during which a
dependent can admit, so a verdict reached before it can be stale by the time the core is public,
and measuring it after the upload closes that window ([#443]). The pre-upload re-check
(``--since-snapshot`` without ``--dispatch``) stays as the release guard — a break exits 1 and
refuses the upload before the dispatch is decided, so there is no verdict line for it. With
``--dispatch`` a break does not refuse: the upload is immutable, so it prints ``live_check=run``
(an admitting dependent exists, so the live check is dispatched) and the break to stderr, and
exits 0 — the break is surfaced as red rather than the release refused.
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


def fetch_requires_dist_for_version(distribution: str, version_str: str) -> list[str] | None:
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
        for version_str, requires_dist in sorted(versions, key=lambda vr: version(vr[0])):
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


def install_and_import(core_wheel: Path, distribution: str, version_str: str) -> str | None:
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
        create = subprocess.run(["uv", "venv", str(venv)], capture_output=True, text=True)
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


def _all_versions(
    by_version: dict[str, list[str]] | None,
) -> list[tuple[str, list[str]]] | None:
    """The all-versions shape: every ``(version, requires_dist)`` pair, or None if unpublished."""
    if by_version is None:
        return None
    return list(by_version.items())


def write_snapshot(path: Path, candidates: list[tuple[str, str]]) -> None:
    """Record the ``(distribution, version)`` pairs build tested, as a JSON list of pairs.

    ``candidates`` is already sorted by ``at_risk`` (distribution then version), so the file is
    stable and diffable. The upload job reads it back with ``read_snapshot`` and re-tests only the
    admitting versions not in it.
    """
    path.write_text(json.dumps([[d, v] for d, v in candidates]))


def read_snapshot(path: Path) -> list[tuple[str, str]]:
    """Load a snapshot written by ``write_snapshot``, as ``list[(distribution, version)]``.

    A missing or malformed snapshot is fatal rather than treated as empty: an empty diff would let
    a real new admitting version ship untested, so a snapshot that cannot be trusted fails closed.
    Raises ``ValueError`` so ``main`` can print the reason and exit 1 without a bare ``SystemExit``
    escaping the test harness.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"snapshot not found at {path}: cannot diff without it") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"snapshot at {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"snapshot at {path} is not a list of [distribution, version] pairs")
    pairs: list[tuple[str, str]] = []
    for entry in raw:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(part, str) for part in entry)
        ):
            raise ValueError(f"snapshot at {path} is not a list of [distribution, version] pairs")
        pairs.append((entry[0], entry[1]))
    return pairs


def newly_admitting(
    current: list[tuple[str, str]], snapshot: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """The admitting versions in ``current`` that are not in ``snapshot``, in ``current``'s order.

    ``current`` is what admits the candidate now; ``snapshot`` is what build tested. A version in
    the snapshot but no longer in ``current`` (yanked again, or its ceiling now excluded — neither
    moves) is ignored: it is not installable now, so it is not a risk to re-test.
    """
    seen = set(snapshot)
    return [pair for pair in current if pair not in seen]


def _usage(prog: str) -> int:
    print(
        f"usage: {prog} <version> <core-wheel> "
        "[--emit-snapshot <path> | --since-snapshot <path>] [--dispatch]",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str]) -> int:
    """CLI entry: build the at-risk candidate set, install and import each against the candidate core wheel, and print OK or FAIL (with optional snapshot diffing)."""
    args = list(argv[1:])
    positionals: list[str] = []
    emit_snapshot: Path | None = None
    since_snapshot: Path | None = None
    dispatch = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--emit-snapshot":
            i += 1
            if i >= len(args):
                return _usage(argv[0])
            emit_snapshot = Path(args[i])
        elif arg == "--since-snapshot":
            i += 1
            if i >= len(args):
                return _usage(argv[0])
            since_snapshot = Path(args[i])
        elif arg == "--dispatch":
            dispatch = True
        else:
            positionals.append(arg)
        i += 1
    if (
        len(positionals) != 2
        or (emit_snapshot is not None and since_snapshot is not None)
        # `--dispatch` derives the verdict after the upload; `--emit-snapshot` records what the
        # build run tested. The two belong to different runs, so combining them is a misuse.
        or (dispatch and emit_snapshot is not None)
    ):
        return _usage(argv[0])
    released = version(positionals[0])
    core_wheel = Path(positionals[1])
    if not core_wheel.is_file():
        print(f"no core wheel at {core_wheel}", file=sys.stderr)
        return 1
    repo_root = Path(__file__).resolve().parent.parent
    distributions = dependent_distributions(repo_root)
    published = {
        distribution: _all_versions(fetch_version_requirements(distribution))
        for distribution in distributions
    }
    candidates = at_risk(published, released)

    if since_snapshot is not None:
        try:
            snapshot = read_snapshot(since_snapshot)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        to_test = newly_admitting(candidates, snapshot)
        if not to_test:
            # The dispatch verdict is this re-check's to emit, not the build run's: the `pypi`
            # environment can hold while the index moves, so the verdict measured at build can be
            # stale by upload (#337). An empty diff is one of two states — still nothing admits
            # (skip), or every admitting version was already tested at build and a published wheel
            # is immutable, so it still imports (run).
            if not candidates:
                print(
                    f"no published dependent admits maf-sandbox {positionals[0]}; nothing to verify"
                )
                print("live_check=skip")
            else:
                print(
                    f"no published dependent admits maf-sandbox {positionals[0]} "
                    "that was not already tested at build; nothing to re-verify"
                )
                print("live_check=run")
            return 0
    else:
        if emit_snapshot is not None:
            write_snapshot(emit_snapshot, candidates)
        to_test = candidates
        if not to_test:
            print(f"no published dependent admits maf-sandbox {positionals[0]}; nothing to verify")
            print("live_check=skip")
            return 0

    failures = breaks(core_wheel, to_test, install_and_import)
    if not failures:
        names = ", ".join(f"{d}=={v}" for d, v in to_test)
        if since_snapshot is not None:
            print(
                f"every published dependent newly admitting maf-sandbox {positionals[0]} "
                f"imports against it ({names})"
            )
            print("live_check=run")
        else:
            print(
                f"every published dependent that admits maf-sandbox {positionals[0]} "
                f"imports against it ({names})"
            )
            print("live_check=run")
        return 0
    for failure in failures:
        print(failure, file=sys.stderr)
    if dispatch:
        # The post-upload dispatch check: the upload already happened and is immutable, so a
        # newly-admitting dependent that breaks cannot be refused. Dispatch the live check
        # (an admitting dependent exists) and surface the break as red rather than silently
        # shipping — the tradeoff #443 accepts over refusing before the upload (#443 option 2
        # would serialize publishes instead). The caller reads stderr for the annotation.
        print(
            f"\nA dependent that admits maf-sandbox {positionals[0]} breaks at import time. "
            "The upload is immutable, so the live check is dispatched (live_check=run) and the "
            "break is surfaced rather than the release refused. See #443.",
            file=sys.stderr,
        )
        print("live_check=run")
        return 0
    print(
        f"\nPublishing maf-sandbox {positionals[0]} breaks the dependents above at import time. "
        "Release a core that keeps them importing, or widen and re-release the dependents first: "
        "RELEASING.md, Release order.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
