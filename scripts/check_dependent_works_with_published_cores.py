"""Refuse to publish a dependent that does not work with the cores it says it supports.

    python scripts/check_dependent_works_with_published_cores.py <distribution> <wheel>

The suite already runs every dependent's tests on every pull request, but against the
**workspace** core — `[tool.uv.sources] maf-sandbox = { workspace = true }`. That pairing is not
the one anybody installs. A consumer gets the dependent's wheel beside whichever *published*
core its range admits, and nothing has ever run that combination before the upload.

So this installs the candidate wheel beside each published core the wheel's own metadata admits,
in a throwaway environment, and runs the package's suite there. The range stops being a claim
nobody checks and becomes one the release has to earn.

Read from the wheel rather than from `pyproject.toml`, because the wheel is what ships: an
`extra-files` edit or a build-backend rewrite that changed the requirement would go unnoticed by
a check that read the source.

Two failures are worth telling apart, and both exit non-zero:

- **A suite fails against a core the range admits.** Either the code is wrong for that core or
  the range claims too much. The floor is the usual culprit — a dependent whose tests need a
  release its floor does not require.
- **No published core is admitted at all.** The wheel is uninstallable as declared, which is
  the shape #175 describes: a range whose ends have drifted past every artifact that exists.

The second is right before an upload and wrong on a pull request, where a change that uses a
new core symbol from a dependent must floor it on a release that does not exist yet (#681).
``--local-core`` answers it with the artifact instead of an argument: when nothing published
is admitted, the suite runs against the core this checkout built, forced past the range with
an override. The claim being tested moves from *"this works beside a core somebody published"*
to *"this works beside the core it ships with"*, which is the only one a pull request can
settle. `publish-packages.yml` passes no flag, so an upload still requires a published core
the range admits.

A network failure is fatal rather than skipped, the same stance as the admit and work checks:
passing because PyPI could not be reached is the one outcome that would make this worthless.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from check_published_dependents_work import fetch_requires_dist_for_version
from check_release_order import admits, fetch_published_versions, version

_CORE = "maf-sandbox"
_ROOT = Path(__file__).resolve().parent.parent

#: The requirement as every dependent spells it. Both ends are captured: the ceiling decides what
#: is admitted, and the floor decides what a consumer may resolve down to — a suite that needs a
#: newer core than the floor requires is a mis-declared floor, and this is where it surfaces.
_RANGE = re.compile(
    rf"{re.escape(_CORE)}\s*>=\s*(?P<floor>\d+(?:\.\d+)*)\s*,\s*<\s*(?P<ceiling>\d+(?:\.\d+)*)"
)

#: Spelled once: two call sites print it, and a usage message that has drifted from the parser
#: is worse than none.
_USAGE = "usage: {program} <distribution> <wheel> [--local-core <wheel>]"

#: What running a suite needs beyond whatever the wheel drags in. The package tests import their
#: own distribution and `maf_sandbox`, both installed here; no suite in this repository uses an
#: async plugin, so pytest alone is the whole of it.
_TEST_REQUIREMENTS = ("pytest",)


def declared_range(wheel: Path) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The floor and ceiling the wheel's own metadata declares on ``maf-sandbox``."""
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if not names:
            raise SystemExit(f"{wheel.name} carries no METADATA — it is not a wheel this can read")
        metadata = archive.read(names[0]).decode("utf-8")
    for line in metadata.splitlines():
        if line.startswith("Requires-Dist:") and _CORE in line:
            if match := _RANGE.search(line):
                return version(match.group("floor")), version(match.group("ceiling"))
    raise SystemExit(
        f"{wheel.name} declares no `{_CORE}>=x,<y` requirement — this check reads the range off "
        "the wheel, so a requirement written another way needs this pattern taught to it"
    )


def admitted_published_cores(floor: tuple[int, ...], ceiling: tuple[int, ...]) -> list[str]:
    """Every published, non-yanked core the range admits, oldest first.

    Oldest first because the floor is where a mis-declared range shows itself, and reporting that
    first is more use than reporting the newest core passing.
    """
    published = fetch_published_versions(_CORE)
    if published is None:
        raise SystemExit(f"{_CORE} has never been published — there is nothing to test against")
    admitted = [
        candidate
        for candidate in published
        if version(candidate) >= floor
        and admits(version(candidate), ceiling)
        and fetch_requires_dist_for_version(_CORE, candidate) is not None
    ]
    return sorted(admitted, key=version)


def sibling_wheels(wheel: Path) -> list[Path]:
    """The other dependents' wheels built beside ``wheel``.

    Two suites reach for a sibling: `maf-sandbox-codeact`'s e2e module imports
    `maf_sandbox_docker`, and `maf-sandbox-docker`'s proxy-parity test asserts outright that
    `maf-sandbox-wslc` is importable so a skip cannot make it vacuous. Both are right to; the
    pairing under test here is *this repository's code* against a core that is published, and a
    sibling is this repository's code. Whether a wheel stands up alone is `smoke_install.py`'s
    question, asked per package in its own environment.
    """
    by_distribution: dict[str, list[Path]] = {}
    for candidate in sorted(wheel.parent.glob("maf_sandbox_*.whl")):
        if candidate.name != wheel.name:
            by_distribution.setdefault(candidate.name.split("-", 1)[0], []).append(candidate)
    # Two wheels for one distribution means a stale artifact is lying around, and choosing
    # between them would decide silently what this run tested. The build job emits one each.
    if ambiguous := {name: found for name, found in by_distribution.items() if len(found) > 1}:
        listed = "; ".join(
            f"{name}: {', '.join(path.name for path in found)}" for name, found in ambiguous.items()
        )
        raise SystemExit(f"more than one wheel for a distribution in {wheel.parent} — {listed}")
    return [found[0] for found in by_distribution.values()]


def run_suite(wheel: Path, core: str | Path, tests: Path) -> tuple[bool, str]:
    """Install ``wheel`` beside ``core`` in a throwaway environment and run ``tests`` there.

    ``core`` is a published version to resolve, or a wheel this checkout built. The wheel is
    forced with ``--overrides``: it carries the version it had before release-please bumped it,
    which is by definition below a floor waiting on the release, and the point is to test the
    code rather than the number.
    """
    with tempfile.TemporaryDirectory() as directory:
        environment = Path(directory) / "venv"
        created = subprocess.run(
            ["uv", "venv", str(environment)], capture_output=True, text=True, check=False
        )
        if created.returncode != 0:
            return False, created.stderr.strip()
        windows = sys.platform == "win32"
        python = (
            environment
            / ("Scripts" if windows else "bin")
            / ("python.exe" if windows else "python")
        )
        if isinstance(core, Path):
            override = Path(directory) / "override.txt"
            override.write_text(f"{_CORE} @ {core.as_uri()}\n", encoding="utf-8")
            pinned = ["--overrides", str(override)]
        else:
            pinned = [f"{_CORE}=={core}"]
        installed = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                str(wheel),
                *(str(sibling) for sibling in sibling_wheels(wheel)),
                *pinned,
                *_TEST_REQUIREMENTS,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if installed.returncode != 0:
            return False, f"the environment would not build:\n{installed.stderr.strip()}"
        # `-p no:cacheprovider` keeps the run from writing a .pytest_cache into the checkout, and
        # the tests are read from this repository because no sdist ships them (see #628).
        ran = subprocess.run(
            [str(python), "-m", "pytest", str(tests), "-q", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            cwd=_ROOT,
            check=False,
        )
        return ran.returncode == 0, (ran.stdout + ran.stderr).strip()


def main(argv: list[str]) -> int:
    """CLI entry: run ``distribution``'s suite against every published core its wheel admits."""
    arguments = argv[1:]
    local_core: Path | None = None
    if "--local-core" in arguments:
        at = arguments.index("--local-core")
        if at + 1 >= len(arguments):
            print(_USAGE.format(program=argv[0]), file=sys.stderr)
            return 2
        # Resolved: the workflow passes `dist/...`, and `as_uri()` below needs an absolute
        # path. Done here rather than at the call site so any caller may pass either.
        local_core = Path(arguments[at + 1]).resolve()
        arguments = arguments[:at] + arguments[at + 2 :]
        if not local_core.is_file():
            print(f"no core wheel at {local_core}", file=sys.stderr)
            return 2
    if len(arguments) != 2:
        print(_USAGE.format(program=argv[0]), file=sys.stderr)
        return 2
    distribution, wheel = arguments[0], Path(arguments[1])
    if distribution == _CORE:
        # A usage error, not a pass: core declares no range on itself, so there is nothing here
        # to check, and exiting 0 would report a check that never ran as a check that succeeded.
        print(f"{_CORE} is the core — this check is for its dependents", file=sys.stderr)
        return 2
    tests = _ROOT / "packages" / distribution / "tests"
    if not tests.is_dir():
        print(f"no test tree at {tests} — nothing to run", file=sys.stderr)
        return 2

    floor, ceiling = declared_range(wheel)
    cores = admitted_published_cores(floor, ceiling)
    span = f">={'.'.join(map(str, floor))},<{'.'.join(map(str, ceiling))}"
    if not cores:
        if local_core is None:
            print(
                f"{distribution} declares {_CORE}{span} and no published {_CORE} satisfies it — "
                "the wheel is uninstallable as declared",
                file=sys.stderr,
            )
            return 1
        passed, output = run_suite(wheel, local_core, tests)
        tail = output.splitlines()[-1] if output else ""
        print(
            f"{'ok  ' if passed else 'FAIL'} {distribution} on the {_CORE} this checkout built "
            f"({local_core.name}): {tail}"
        )
        if passed:
            print(
                f"     nothing published satisfies {_CORE}{span} yet, so the pairing tested is "
                "this code against its own core. The index is what the upload is checked against."
            )
            return 0
        print(output, file=sys.stderr)
        return 1

    failures = 0
    for core in cores:
        passed, output = run_suite(wheel, core, tests)
        tail = output.splitlines()[-1] if output else ""
        print(f"{'ok  ' if passed else 'FAIL'} {distribution} on {_CORE} {core}: {tail}")
        if not passed:
            failures += 1
            print(output, file=sys.stderr)
    if failures:
        print(
            f"{distribution}'s suite fails against {failures} of {len(cores)} published core(s) "
            f"its range admits ({span}) — either the code is wrong for that core or the range "
            "claims more than it can keep",
            file=sys.stderr,
        )
        return 1
    print(f"OK  {distribution} works with every published {_CORE} it admits ({span})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
