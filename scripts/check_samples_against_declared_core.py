"""Refuse a sample that cannot run on the core its own PEP 723 block names.

    python scripts/check_samples_against_declared_core.py [--local-core <wheel>] [sample ...]

`tests/test_sample_metadata.py` checks a floor's shape and its release window;
`tests/test_sample_modules_import.py` imports every sample against the **workspace** core. So
the floor is checked without the imports and the imports are checked without the floor, and a
sample using a symbol its floor does not carry passes both (#725).

This resolves each block the way a capped reader's install would — the oldest published core
the floor admits — and type-checks the samples there. pyright rather than an import, because
an attribute on a returned value is reachable from neither an import nor module level.

``--local-core`` is the release-window escape and fires only when nothing published satisfies
the floor. It substitutes every wheel `dist/` holds, not the core alone.

`docs/release-compatibility.md` carries the reasoning, the boundaries and when each refusal is
reachable. `check_dependent_works_with_published_cores.py` is the packages' counterpart.

An index still unreachable after `pypi_index`'s retries is fatal rather than skipped, the same
stance as the checks it sits beside.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from check_dependent_works_with_published_cores import sibling_wheels, throwaway_interpreter
from check_published_dependents_work import fetch_requires_dist_for_version
from check_release_order import fetch_published_versions, version
from pypi_index import run_check
from sample_blocks import declared, distribution, sample_directories

_CORE = "maf-sandbox"
_ROOT = Path(__file__).resolve().parent.parent

#: The core floor, in the bare shape `scripts/set_dependents_range.py` edits.
_CORE_FLOOR = re.compile(rf"^{re.escape(_CORE)}\s*>=\s*(\d+(?:\.\d+)*)$")

#: A `requires-python` this can turn into a pyright `--pythonversion`. Anything else is left to
#: the interpreter, because guessing a version is worse than inheriting one.
_PYTHON_FLOOR = re.compile(r">=\s*(\d+\.\d+)")

#: What pyright drops by default, and therefore what `expected_files` must not count. A `.venv`
#: or a `.mypy_cache` under a sample would otherwise make a complete pass look like a short one.
_PYRIGHT_EXCLUDES = ("node_modules", "__pycache__")

_USAGE = "usage: {program} [--local-core <wheel>] [sample ...]"


def metadata(sample: Path) -> dict:
    """The PEP 723 block `uv run agent.py` reads."""
    block = declared(sample / "agent.py")
    if block is None:
        raise SystemExit(f"{sample.name}/agent.py has no PEP 723 block")
    return block


def named_distribution(requirement: str) -> str:
    """The distribution ``requirement`` names."""
    found = distribution(requirement)
    if found is None:
        raise SystemExit(f"no distribution name in {requirement!r}")
    return found


def core_floor(sample: Path) -> tuple[int, ...]:
    """The `maf-sandbox` floor a sample declares."""
    for dependency in metadata(sample)["dependencies"]:
        stripped = dependency.strip()
        if named_distribution(stripped) != _CORE:
            continue
        match = _CORE_FLOOR.match(stripped)
        if not match:
            raise SystemExit(
                f"{sample.name} declares {stripped!r}; the floor has to be a bare "
                f"`{_CORE}>=X`, the shape scripts/set_dependents_range.py edits"
            )
        return version(match.group(1))
    raise SystemExit(f"{sample.name} names no {_CORE} floor")


def other_requirements(sample: Path) -> list[str]:
    """Everything the block declares except the core, whose version this check decides."""
    return [
        dependency.strip()
        for dependency in metadata(sample)["dependencies"]
        if named_distribution(dependency) != _CORE
    ]


def python_floor(samples: list[Path]) -> str | None:
    """The oldest Python any of ``samples`` claims to run on, as pyright spells a version.

    Read so the analysis version is the sample's claim rather than whichever interpreter `uv
    venv` happened to find, which differs between a contributor's machine and the runner.
    """
    floors = [
        match.group(1)
        for sample in samples
        if (match := _PYTHON_FLOOR.search(str(metadata(sample).get("requires-python", ""))))
    ]
    return min(floors, key=lambda floor: tuple(map(int, floor.split(".")))) if floors else None


def lowest_admitted_core(floor: tuple[int, ...]) -> str | None:
    """The oldest published, non-yanked core the floor admits, or None if it admits none.

    The oldest rather than the newest, because the floor's claim is about its lower end: an
    unpinned reader resolves to the newest and never exercises it, and a consumer capped
    elsewhere lands here. Reached by comparison rather than by an `==` pin, so `>=0.25` finds
    `0.25.1` when no `0.25.0` was released — which release-please produces whenever a fix and a
    feature land in one Release PR.

    Oldest-first so the per-version document is fetched until one answers rather than for every
    candidate above the floor.
    """
    published = fetch_published_versions(_CORE)
    if published is None:
        raise SystemExit(f"{_CORE} has never been published — there is nothing to check against")
    for candidate in sorted(published, key=version):
        if version(candidate) >= floor:
            if fetch_requires_dist_for_version(_CORE, candidate) is not None:
                return candidate
    return None


def built_here(core: Path) -> list[tuple[str, Path]]:
    """Every distribution this checkout built beside ``core``, as ``(name, wheel)``."""
    return [
        (wheel.name.split("-", 1)[0].replace("_", "-"), wheel)
        for wheel in [core, *sibling_wheels(core)]
    ]


def install_arguments(directory: Path, requirements: list[str], core: str | Path) -> list[str]:
    """What to ask `uv pip install` for, so that the core under test is one of the operands.

    An override rewrites a requirement; it never adds one. The core has to be *requested* or a
    group whose samples name no backend installs nothing at all, and one that names a backend
    gets the core only because the backend drags it in.
    """
    if isinstance(core, Path):
        override = directory / "override.txt"
        override.write_text(
            "".join(f"{name} @ {wheel.as_uri()}\n" for name, wheel in built_here(core)),
            encoding="utf-8",
        )
        return ["--overrides", str(override), _CORE, *requirements]
    return [f"{_CORE}=={core}", *requirements]


def build_environment(directory: Path, requirements: list[str], core: str | Path) -> Path | str:
    """Install ``requirements`` beside ``core`` in a throwaway environment; answer its python.

    A string comes back in place of a path when the environment would not build, and it is the
    reason.
    """
    python = throwaway_interpreter(directory)
    if isinstance(python, str):
        return python
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            *install_arguments(directory, requirements, core),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if installed.returncode != 0:
        return f"the environment would not build:\n{installed.stderr.strip()}"
    return python


def resolved_family(python: Path) -> str:
    """What this repository's distributions actually resolved to, for the report line.

    The core is pinned and its siblings are not, so satisfying the pin can walk a backend
    backwards. Naming what was installed is what stops a backend's error being read as the
    core's.
    """
    listed = subprocess.run(
        ["uv", "pip", "list", "--python", str(python), "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0 or not listed.stdout.strip():
        return "the installed versions could not be read"
    family = [
        f"{package['name']} {package['version']}"
        for package in json.loads(listed.stdout)
        if package["name"].startswith(_CORE)
    ]
    return ", ".join(sorted(family)) or "nothing from this repository"


def write_config(directory: Path, samples: list[Path]) -> Path:
    """A pyright config outside the repository, so the root `[tool.pyright]` does not answer.

    An execution environment per sample puts each directory on its own import path, which is
    what `sys.path[0]` does when `uv run agent.py` runs it.
    """
    config = directory / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "typeCheckingMode": "standard",
                "executionEnvironments": [{"root": sample.as_posix()} for sample in samples],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return config


def modules_under(sample: Path) -> list[Path]:
    """The modules pyright will read under ``sample``, by pyright's own default exclusions."""
    return [
        module
        for module in sample.rglob("*.py")
        if not any(
            part.startswith(".") or part in _PYRIGHT_EXCLUDES
            for part in module.relative_to(sample).parts
        )
    ]


def expected_files(samples: list[Path]) -> int:
    """How many modules pyright has to report reading before its silence means anything."""
    return sum(len(modules_under(sample)) for sample in samples)


def errors_by_sample(report: dict, samples: list[Path]) -> dict[str, list[str]]:
    """Each sample's errors from one pyright report; every sample is a key.

    A sample that passed and a sample nothing read look identical in a report that only lists
    diagnostics, so the keys come from the request rather than from the answer. Warnings are
    not errors here: the question is whether the sample works.
    """
    found: dict[str, list[str]] = {sample.name: [] for sample in samples}
    for diagnostic in report["generalDiagnostics"]:
        if diagnostic["severity"] != "error":
            continue
        path = Path(diagnostic["file"])
        owner = next((sample for sample in samples if sample in path.parents), None)
        if owner is None:
            continue
        rule = diagnostic.get("rule") or "error"
        found[owner.name].append(f"{path.name}: {rule}: {diagnostic['message'].splitlines()[0]}")
    return found


def type_check(samples: list[Path], python: Path, config: Path) -> dict[str, list[str]]:
    """Run pyright over ``samples`` against ``python``; answer each one's errors."""
    pinned = python_floor(samples)
    ran = subprocess.run(
        [
            "uv",
            "run",
            "pyright",
            "-p",
            str(config),
            "--pythonpath",
            str(python),
            *(["--pythonversion", pinned] if pinned else []),
            "--outputjson",
            *(str(sample) for sample in samples),
        ],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        check=False,
    )
    # The exit code says nothing — pyright exits non-zero *because* it found something. Empty
    # output is the failure worth reporting, and it means the binary never ran.
    if "{" not in ran.stdout:
        raise SystemExit(
            f"pyright produced no output (exit {ran.returncode}): "
            f"{ran.stderr.strip()[:500] or '(empty)'}"
        )
    report = json.loads(ran.stdout[ran.stdout.find("{") :])
    analysed, expected = report["summary"]["filesAnalyzed"], expected_files(samples)
    if analysed < expected:
        raise SystemExit(
            f"pyright read {analysed} of the {expected} modules under {len(samples)} sample(s) — "
            "a pass that reads less than it was pointed at reports green for the rest"
        )
    return errors_by_sample(report, samples)


def printed(floor: tuple[int, ...]) -> str:
    """A floor as the block writes it."""
    return ".".join(str(part) for part in floor)


def check_group(
    floor: tuple[int, ...], samples: list[Path], local_core: Path | None
) -> tuple[int, str | None]:
    """Check samples declaring one block. Answer its failures, or why none could be run."""
    core: str | Path | None = lowest_admitted_core(floor)
    if core is None:
        if local_core is None:
            return 0, (
                f"{len(samples)} sample(s) declare {_CORE}>={printed(floor)} and no published "
                f"{_CORE} satisfies it — they are unresolvable as declared. Release the core "
                "first, or pass --local-core to check them against the one this checkout built."
            )
        core = local_core
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        requirements = sorted({found for sample in samples for found in other_requirements(sample)})
        python = build_environment(workspace, requirements, core)
        if isinstance(python, str):
            return 0, f"{_CORE}>={printed(floor)}: {python}"
        family = resolved_family(python)
        errors = type_check(samples, python, write_config(workspace, samples))
    print(f"[{'dist/' if isinstance(core, Path) else 'index'}: {family}]")
    failures = 0
    for sample in samples:
        reported = errors[sample.name]
        print(f"  {'ok  ' if not reported else 'FAIL'} {sample.name}")
        for line in reported:
            print(f"         {line}")
        failures += bool(reported)
    return failures, None


def selected(arguments: list[str]) -> list[Path] | str:
    """The samples named on the command line, or all of them; a string is the refusal.

    Deduplicated: naming one twice would double what `expected_files` demands while pyright
    reads it once, and the short-read guard would then accuse pyright of the difference.
    """
    every = sample_directories()
    if not every:
        return f"no samples under {_ROOT / 'samples'} — this check would pass vacuously"
    if not arguments:
        return every
    by_name = {sample.name: sample for sample in every}
    unknown = [name for name in arguments if Path(name).name not in by_name]
    if unknown:
        return f"no such sample: {', '.join(unknown)}"
    chosen = {by_name[Path(name).name] for name in arguments}
    return [sample for sample in every if sample in chosen]


def read_local_core(arguments: list[str]) -> tuple[list[str], Path | None, str | None]:
    """Take ``--local-core`` off the command line. A string is the refusal."""
    if "--local-core" not in arguments:
        return arguments, None, None
    at = arguments.index("--local-core")
    if at + 1 >= len(arguments):
        return arguments, None, _USAGE
    wheel = Path(arguments[at + 1]).resolve()
    rest = arguments[:at] + arguments[at + 2 :]
    if not wheel.is_file():
        return rest, None, f"no core wheel at {wheel}"
    if wheel.name.split("-", 1)[0].replace("_", "-") != _CORE:
        # `sibling_wheels` globs `maf_sandbox_*`, which by construction never matches the core.
        # A backend wheel here would substitute every sibling and leave the core alone.
        return rest, None, f"{wheel.name} is not a {_CORE} wheel"
    return rest, wheel, None


def main(argv: list[str]) -> int:
    """CLI entry: type-check each named sample, or all of them, against its declared core."""
    arguments, local_core, refused = read_local_core(argv[1:])
    if refused is not None:
        print(refused.format(program=argv[0]), file=sys.stderr)
        return 2

    every = selected(arguments)
    if isinstance(every, str):
        print(every, file=sys.stderr)
        return 2

    # By the whole block, not by the floor alone: an environment built from several samples'
    # requirements is one no sample declared, and a bare backend requirement in one would then
    # be checked at another's floor.
    groups: dict[tuple, list[Path]] = {}
    for sample in every:
        floor = core_floor(sample)
        groups.setdefault((floor, tuple(other_requirements(sample))), []).append(sample)

    failures, refusals = 0, 0
    for floor, _requirements in sorted(groups):
        failed, refusal = check_group(floor, groups[(floor, _requirements)], local_core)
        failures += failed
        if refusal:
            refusals += 1
            print(refusal, file=sys.stderr)
    if failures:
        print(
            f"{failures} of {len(every)} sample(s) do not work against the core their own block "
            "names. Either the floor is wrong or the sample is — and after a core release "
            "`python scripts/set_dependents_range.py <released-version>` moves every floor.",
            file=sys.stderr,
        )
    if failures or refusals:
        return 1
    print(f"OK  {len(every)} sample(s) work against the core each one's block names")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_check(main, sys.argv))
