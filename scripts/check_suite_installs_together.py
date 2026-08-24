"""Prove the suite still forms a set somebody can install, and say which set that is.

    python scripts/check_suite_installs_together.py [--dist-dir dist] [--whole-set]

The dependents are installed together, not one at a time: `samples/03` takes
`maf-sandbox-acas` and `maf-sandbox-codeact` beside the core, `samples/11` takes
`maf-sandbox-bicep` and `maf-sandbox-docker`. So a dependent sitting on an older core line is
fine on its own, and two dependents whose constraints stop overlapping are not — nothing can
put them in one environment.

Intersecting the ranges by hand would answer a narrower question than the one that matters.
These packages also carry `agent-framework-core` and the Azure libraries, and two of them can
become mutually uninstallable over those while their core ranges overlap perfectly. So the
resolver is asked instead, and asked to *install* rather than merely resolve: resolution is
metadata, installation is artifacts, and a set can resolve cleanly and still fail on a wheel
that is not there.

Two modes, because a release has two shapes:

- **per-candidate** (the default) — this checkout's wheel for one dependent, beside whatever
  the resolver picks for the others from the index. That is what a consumer gets the day that
  one package is published and the rest are not.
- **whole-set** (`--whole-set`) — every wheel in the directory together, no published
  fallbacks. That is what a release covering several packages has to satisfy.

It passes on the existential and **reports the set that resolved**. "Latest of everything" and
"had to go back four versions" are both installable, and the difference is the drift worth
seeing before it becomes a support question.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from check_published_dependents_admit import dependent_distributions
from check_release_order import fetch_published_versions, version

_CORE = "maf-sandbox"
_ROOT = Path(__file__).resolve().parent.parent


def _python_in(environment: Path) -> Path:
    """The interpreter a `uv venv` at ``environment`` created."""
    windows = sys.platform == "win32"
    return environment / ("Scripts" if windows else "bin") / ("python.exe" if windows else "python")


def wheels_in(dist_dir: Path) -> dict[str, Path]:
    """Every `maf-sandbox*` wheel in ``dist_dir`` by distribution name, refusing duplicates."""
    found: dict[str, list[Path]] = {}
    for wheel in sorted(dist_dir.glob("maf_sandbox*.whl")):
        found.setdefault(wheel.name.split("-", 1)[0].replace("_", "-"), []).append(wheel)
    if ambiguous := {name: paths for name, paths in found.items() if len(paths) > 1}:
        listed = "; ".join(f"{n}: {', '.join(p.name for p in ps)}" for n, ps in ambiguous.items())
        raise SystemExit(f"more than one wheel for a distribution in {dist_dir} — {listed}")
    return {name: paths[0] for name, paths in found.items()}


def install(requirements: list[str]) -> tuple[bool, dict[str, str], str]:
    """Install ``requirements`` into a throwaway environment; return what landed in it."""
    with tempfile.TemporaryDirectory() as directory:
        environment = Path(directory) / "venv"
        created = subprocess.run(
            ["uv", "venv", str(environment)], capture_output=True, text=True, check=False
        )
        if created.returncode != 0:
            return False, {}, created.stderr.strip()
        python = _python_in(environment)
        installed = subprocess.run(
            ["uv", "pip", "install", "--python", str(python), *requirements],
            capture_output=True,
            text=True,
            check=False,
        )
        if installed.returncode != 0:
            tail = [line for line in installed.stderr.strip().splitlines() if line.strip()]
            return False, {}, "\n".join(tail[-4:])
        listed = subprocess.run(
            ["uv", "pip", "list", "--python", str(python), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if listed.returncode != 0:
            return True, {}, ""
        resolved = {
            entry["name"]: entry["version"]
            for entry in json.loads(listed.stdout)
            if entry["name"].startswith(_CORE)
        }
        return True, resolved, ""


def latest_published() -> dict[str, str]:
    """The newest published version of the core and each dependent, for the drift report."""
    newest: dict[str, str] = {}
    for distribution in [_CORE, *dependent_distributions(_ROOT)]:
        published = fetch_published_versions(distribution)
        if published:
            newest[distribution] = sorted(published, key=version)[-1]
    return newest


def report(resolved: dict[str, str], newest: dict[str, str], indent: str = "    ") -> list[str]:
    """The resolved set, marking anything the resolver had to go back for."""
    lines = []
    for name in sorted(resolved):
        behind = (
            "  <- not the newest published"
            if name in newest and version(resolved[name]) < version(newest[name])
            else ""
        )
        lines.append(f"{indent}{name:<22} {resolved[name]}{behind}")
    return lines


def main(argv: list[str]) -> int:
    """CLI entry: install the suite as a set and print what the resolver chose."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=_ROOT / "dist")
    parser.add_argument("--whole-set", action="store_true")
    parsed = parser.parse_args(argv[1:])

    wheels = wheels_in(parsed.dist_dir)
    if not wheels:
        print(f"no wheels in {parsed.dist_dir} — build them first", file=sys.stderr)
        return 2
    newest = latest_published()

    if parsed.whole_set:
        passed, resolved, error = install([str(path) for path in wheels.values()])
        print(f"{'ok  ' if passed else 'FAIL'} whole set: {len(wheels)} wheel(s) together")
        print(
            "\n".join(report(resolved, newest)) if passed else error,
            file=sys.stderr if not passed else sys.stdout,
        )
        if not passed:
            print(
                "the release's own packages cannot be installed together — a range moved past a "
                "sibling rather than past the index",
                file=sys.stderr,
            )
            return 1
        return 0

    failures = 0
    for distribution, wheel in wheels.items():
        if distribution == _CORE:
            continue
        others = [name for name in wheels if name not in (distribution, _CORE)]
        passed, resolved, error = install([str(wheel), *others])
        print(f"{'ok  ' if passed else 'FAIL'} {distribution} beside the published others")
        if passed:
            print("\n".join(report(resolved, newest)))
        else:
            failures += 1
            print(error, file=sys.stderr)
    if failures:
        print(
            f"{failures} candidate(s) cannot be installed beside the published suite — publishing "
            "one would leave the family unresolvable as a set",
            file=sys.stderr,
        )
        return 1
    print("OK  every candidate installs beside the published suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
