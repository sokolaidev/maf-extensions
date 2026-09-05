"""Run the dependents' suites against a candidate core, published ones and this branch's alike.

    python scripts/check_core_against_dependents.py <version> <core-wheel> [--dist-dir dist]

`check_published_dependents_work.py` asks whether each published dependent still *imports* the
candidate core. That is a real gate in the right place and it proves very little: a changed
signature, a moved default or a renamed keyword all import clean and ship. This runs their
tests instead.

Two halves, and the difference between them is the point:

- **published** — every published dependent version whose ceiling admits the candidate. Its
  suite is recovered from that version's release tag, because no sdist ships tests, and run
  against the published wheel. This is what is installed today.
- **branch** — each dependent as it stands in this checkout, built and run against the same
  core. This is what is *about to be* installed.

A breaking core makes them disagree, and the disagreement is the useful part. Published failing
while branch passes says the break is real and already handled — those packages simply have to
publish. Both failing says nothing has handled it yet. Only the second is a reason to reconsider
the change rather than the order.

Both halves refuse. The escape from a published-half refusal is not to weaken the gate: it is to
release at a version *outside* those ceilings, with `Release-As:` in the commit footer, so the
break is out of reach of everything already installed and each dependent adopts on its own
schedule. See `docs/release-compatibility.md`.

A sibling comes from this checkout in both halves. Two suites import one, and a published
sibling would drag its own bound on the core into the resolution — which is a constraint about
that sibling's release, not about the core under test.

The core is forced with `--overrides` in both halves, never resolved, and that is the same
sentence one step further: a branch sibling carries a bound too. Every wheel in the environment
declares a `maf-sandbox` range, and a range says which cores that package is *published*
against rather than whether its tests pass beside this one. Resolved instead, an in-tree
ceiling still a cycle behind refuses to build the environment at all, and this reports that in
the shape of a failing suite — over a version RELEASING.md permits a core to be released at,
and for a breaking release intends it to be. `tests.yml`, `check_suite_installs_together.py`
and `check_dependent_works_with_published_cores.py` force it for the same reason.

An index still unreachable after `pypi_index`'s retries is fatal rather than skipped, the same
stance as the admit and import checks.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from check_published_dependents_admit import dependent_distributions
from check_published_dependents_work import at_risk, fetch_version_requirements
from pypi_index import run_check, version

_CORE = "maf-sandbox"
_ROOT = Path(__file__).resolve().parent.parent
_TEST_REQUIREMENTS = ("pytest",)


@dataclass(frozen=True)
class Result:
    """One suite run: which half it belongs to, what it ran against, and how it went."""

    half: str
    distribution: str
    against: str
    passed: bool
    summary: str

    def line(self) -> str:
        """The row this prints, with the verdict first so a failure is findable in a log."""
        return f"{'ok  ' if self.passed else 'FAIL'} {self.half:<9} {self.distribution} {self.against}: {self.summary}"


def _python_in(environment: Path) -> Path:
    """The interpreter a `uv venv` at ``environment`` created."""
    windows = sys.platform == "win32"
    return environment / ("Scripts" if windows else "bin") / ("python.exe" if windows else "python")


def dependent_wheels(dist_dir: Path) -> dict[str, Path]:
    """The dependents' wheels in ``dist_dir``, by distribution name.

    Refuses two wheels for one distribution rather than choosing: a stale artifact would decide
    silently what a run had tested.
    """
    found: dict[str, list[Path]] = {}
    for wheel in sorted(dist_dir.glob("maf_sandbox_*.whl")):
        found.setdefault(wheel.name.split("-", 1)[0].replace("_", "-"), []).append(wheel)
    if ambiguous := {name: paths for name, paths in found.items() if len(paths) > 1}:
        listed = "; ".join(f"{n}: {', '.join(p.name for p in ps)}" for n, ps in ambiguous.items())
        raise SystemExit(f"more than one wheel for a distribution in {dist_dir} — {listed}")
    return {name: paths[0] for name, paths in found.items()}


def recover_tests(tag: str, distribution: str, into: Path) -> Path | None:
    """Extract ``distribution``'s test tree as it stood at ``tag``, or None if the tag is absent.

    From the tag rather than from PyPI because no sdist in this repository ships its tests, so a
    published version's suite exists only here. `git archive` rather than a worktree: nothing
    needs a checkout, and the tests import nothing from outside their own tree.
    """
    listed = subprocess.run(
        ["git", "tag", "--list", tag], cwd=_ROOT, capture_output=True, text=True, check=False
    )
    if listed.returncode != 0 or not listed.stdout.strip():
        return None
    into.mkdir(parents=True, exist_ok=True)
    archived = subprocess.run(
        ["git", "archive", tag, f"packages/{distribution}/tests"],
        cwd=_ROOT,
        capture_output=True,
        check=False,
    )
    if archived.returncode != 0:
        return None
    extracted = subprocess.run(
        ["tar", "-x", "-C", str(into)], input=archived.stdout, capture_output=True, check=False
    )
    if extracted.returncode != 0:
        return None
    tests = into / "packages" / distribution / "tests"
    return tests if tests.is_dir() else None


def run_suite(requirements: list[str], core: Path, tests: Path) -> tuple[bool, str]:
    """Install ``requirements`` beside ``core`` in a throwaway environment and run ``tests``.

    ``core`` is both an operand and an override, because an override rewrites a requirement and
    never adds one: the wheel has to be asked for as well as forced. Naming it here rather than
    in each caller's list is what keeps the two from drifting apart.
    """
    with tempfile.TemporaryDirectory() as directory:
        environment = Path(directory) / "venv"
        created = subprocess.run(
            ["uv", "venv", str(environment)], capture_output=True, text=True, check=False
        )
        if created.returncode != 0:
            return False, created.stderr.strip().splitlines()[-1] if created.stderr else "no venv"
        python = _python_in(environment)
        override = Path(directory) / "override.txt"
        override.write_text(f"{_CORE} @ {core.as_uri()}\n", encoding="utf-8")
        installed = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--overrides",
                str(override),
                *requirements,
                str(core),
                *_TEST_REQUIREMENTS,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if installed.returncode != 0:
            tail = installed.stderr.strip().splitlines()
            return False, "the environment would not build: " + (tail[-1] if tail else "")
        ran = subprocess.run(
            [str(python), "-m", "pytest", str(tests), "-q", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            cwd=_ROOT,
            check=False,
        )
        output = (ran.stdout + ran.stderr).strip().splitlines()
        return ran.returncode == 0, output[-1] if output else ""


def assess_branch(core_wheel: Path, wheels: dict[str, Path]) -> list[Result]:
    """Each dependent as this checkout has it, against the candidate core."""
    results = []
    for distribution, wheel in wheels.items():
        siblings = [str(other) for name, other in wheels.items() if name != distribution]
        passed, summary = run_suite(
            [str(wheel), *siblings], core_wheel, _ROOT / "packages" / distribution / "tests"
        )
        results.append(Result("branch", distribution, "this checkout", passed, summary))
    return results


def assess_published(
    released: tuple[int, ...], core_wheel: Path, wheels: dict[str, Path], scratch: Path
) -> list[Result]:
    """Every published dependent version whose ceiling admits the candidate, at its own tag."""
    published = {
        distribution: (
            None
            if (found := fetch_version_requirements(distribution)) is None
            else sorted(found.items())
        )
        for distribution in dependent_distributions(_ROOT)
    }
    results = []
    for distribution, released_version in at_risk(published, released):
        tag = f"{distribution}-v{released_version}"
        tests = recover_tests(tag, distribution, scratch / tag)
        if tests is None:
            results.append(
                Result("published", distribution, released_version, False, f"no test tree at {tag}")
            )
            continue
        siblings = [str(other) for name, other in wheels.items() if name != distribution]
        passed, summary = run_suite(
            [f"{distribution}=={released_version}", *siblings], core_wheel, tests
        )
        results.append(Result("published", distribution, released_version, passed, summary))
    return results


def main(argv: list[str]) -> int:
    """CLI entry: run both halves against the candidate core and refuse on any failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("released")
    parser.add_argument("core_wheel", type=Path)
    parser.add_argument("--dist-dir", type=Path, default=_ROOT / "dist")
    parsed = parser.parse_args(argv[1:])

    wheels = dependent_wheels(parsed.dist_dir)
    if not wheels:
        print(f"no dependent wheels in {parsed.dist_dir} — build them first", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as scratch:
        results = assess_branch(parsed.core_wheel, wheels) + assess_published(
            version(parsed.released), parsed.core_wheel, wheels, Path(scratch)
        )
        for result in results:
            print(result.line())

    if not any(result.half == "published" for result in results):
        # Not a pass and not a failure: it is the window before any dependent admits the
        # candidate, which the live-check dispatch already treats as its own state (#273).
        print(f"no published dependent admits {_CORE} {parsed.released} — nothing at risk yet")

    failed = [result for result in results if not result.passed]
    if failed:
        halves = {result.half for result in failed}
        if halves == {"published"}:
            print(
                "every failure is in the published half, so the break is real and already "
                "handled here — release outside those ceilings with `Release-As:` rather than "
                "weakening this, and let each dependent adopt on its own schedule",
                file=sys.stderr,
            )
        print(f"{len(failed)} suite(s) failed against {_CORE} {parsed.released}", file=sys.stderr)
        return 1
    print(f"OK  every dependent's suite passes against {_CORE} {parsed.released}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_check(main, sys.argv))
