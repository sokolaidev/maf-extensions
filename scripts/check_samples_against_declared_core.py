"""Refuse a sample that cannot run on the core its own PEP 723 block names.

    python scripts/check_samples_against_declared_core.py [--local-core <wheel>] [sample ...]

`tests/test_sample_metadata.py` checks a floor's shape and its release window;
`tests/test_sample_modules_import.py` imports every sample against the **workspace** core. So
the floor is checked without the imports and the imports are checked without the floor, and a
sample using a symbol its floor does not carry passes both (#725).

This resolves the floor the way a capped reader's install would — the oldest published core the
floor admits — puts it beside the rest of the block, and type-checks the samples against it.
pyright rather than an import: what broke last time was `purge.disposed`, an attribute on a
returned object, which no import reaches.

``--local-core`` is the release-window escape and fires only when nothing published satisfies
the floor: the samples are checked against every wheel this checkout built rather than against
the index. Its siblings come too, because a core the window is waiting on is one the published
backends do not implement yet. `docs/release-compatibility.md` carries the reasoning, and
`check_dependent_works_with_published_cores.py` is the packages' counterpart, where the flag
means the same thing.

A network failure is fatal rather than skipped, the same stance as the checks it sits beside.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from check_dependent_works_with_published_cores import sibling_wheels
from check_published_dependents_work import fetch_requires_dist_for_version
from check_release_order import fetch_published_versions, version

_CORE = "maf-sandbox"
_ROOT = Path(__file__).resolve().parent.parent
_SAMPLES = _ROOT / "samples"

#: PEP 723, as the spec writes it. The same shape the two suites above parse.
_BLOCK = re.compile(r"(?m)^# /// script\s*$\s(?P<body>(?:^#(?:| .*)$\s)+)^# ///\s*$")

#: The distribution a requirement names, before any specifier or extra.
_DISTRIBUTION = re.compile(r"[A-Za-z0-9._-]+")

#: The core floor, in the bare shape `scripts/set_dependents_range.py` edits.
_CORE_FLOOR = re.compile(rf"^{re.escape(_CORE)}\s*>=\s*(\d+(?:\.\d+)*)$")

_USAGE = "usage: {program} [--local-core <wheel>] [sample ...]"


def sample_directories() -> list[Path]:
    """Every sample directory, in the order their numbers give."""
    return sorted(path for path in _SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())


def distribution(requirement: str) -> str:
    """The distribution a requirement names, before any specifier or extra."""
    match = _DISTRIBUTION.match(requirement.strip())
    if not match:
        raise SystemExit(f"no distribution name in {requirement!r}")
    return match.group(0)


def metadata(sample: Path) -> dict:
    """The PEP 723 block `uv run agent.py` reads, as the TOML the spec says it is."""
    match = _BLOCK.search((sample / "agent.py").read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"{sample.name}/agent.py has no PEP 723 block")
    body = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("body").splitlines(keepends=True)
    )
    return tomllib.loads(body)


def core_floor(sample: Path) -> tuple[int, ...]:
    """The `maf-sandbox` floor a sample declares."""
    for dependency in metadata(sample)["dependencies"]:
        stripped = dependency.strip()
        if distribution(stripped) != _CORE:
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
        if distribution(dependency) != _CORE
    ]


def lowest_admitted_core(floor: tuple[int, ...]) -> str | None:
    """The oldest published, non-yanked core the floor admits, or None if it admits none.

    The oldest rather than the newest, because the floor's claim is about its lower end: an
    unpinned reader resolves to the newest and never exercises it, and a consumer capped
    elsewhere lands here. Reached by comparison rather than by an `==` pin, so `>=0.25` finds
    `0.25.1` when no `0.25.0` was released — which release-please produces whenever a fix and a
    feature land in one Release PR.
    """
    published = fetch_published_versions(_CORE)
    if published is None:
        raise SystemExit(f"{_CORE} has never been published — there is nothing to check against")
    admitted = [
        candidate
        for candidate in published
        if version(candidate) >= floor
        and fetch_requires_dist_for_version(_CORE, candidate) is not None
    ]
    return min(admitted, key=version) if admitted else None


def built_here(core: Path) -> list[tuple[str, Path]]:
    """Every distribution this checkout built beside ``core``, as ``(name, wheel)``.

    The siblings come along because the window ``--local-core`` exists for is the one where the
    core is unreleased, and a published backend cannot implement a protocol it predates: paired
    with an unreleased core it fails as a *sample* error, a diagnosis nobody can act on. During
    that window the dependents are unreleased too, and `dist/` holds every one of them.
    """
    return [
        (wheel.name.split("-", 1)[0].replace("_", "-"), wheel)
        for wheel in [core, *sibling_wheels(core)]
    ]


def build_environment(directory: Path, requirements: list[str], core: str | Path) -> Path | str:
    """Install ``requirements`` beside ``core`` in a throwaway environment; answer its python.

    ``core`` is a published version to pin, or a wheel this checkout built — which is forced
    with ``--overrides``, along with its siblings, because the requirement strings resolve from
    the index and the point here is to check the code rather than the numbers. A string comes
    back in place of a path when the environment would not build, and it is the reason.
    """
    environment = directory / "venv"
    created = subprocess.run(
        ["uv", "venv", str(environment)], capture_output=True, text=True, check=False
    )
    if created.returncode != 0:
        return created.stderr.strip()
    windows = sys.platform == "win32"
    python = (
        environment / ("Scripts" if windows else "bin") / ("python.exe" if windows else "python")
    )
    if isinstance(core, Path):
        override = directory / "override.txt"
        override.write_text(
            "".join(f"{name} @ {wheel.as_uri()}\n" for name, wheel in built_here(core)),
            encoding="utf-8",
        )
        pinned = ["--overrides", str(override)]
    else:
        pinned = [f"{_CORE}=={core}"]
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), *requirements, *pinned],
        capture_output=True,
        text=True,
        check=False,
    )
    if installed.returncode != 0:
        return f"the environment would not build:\n{installed.stderr.strip()}"
    return python


def write_config(directory: Path, samples: list[Path]) -> Path:
    """A pyright config outside the repository, so the root `[tool.pyright]` does not answer.

    An execution environment per sample puts each directory on its own import path, which is
    what `sys.path[0]` does when `uv run agent.py` runs it. Without them a sample's
    `from _scaffold import …` fails for a reason that is not the core's.
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


def expected_files(samples: list[Path]) -> int:
    """How many modules pyright has to report reading before its silence means anything."""
    return sum(len(list(sample.rglob("*.py"))) for sample in samples)


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
    ran = subprocess.run(
        [
            "uv",
            "run",
            "pyright",
            "-p",
            str(config),
            "--pythonpath",
            str(python),
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
    """Check every sample declaring ``floor``. Answer its failures, or why none could be run."""
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
        errors = type_check(samples, python, write_config(workspace, samples))
    named = "the wheels this checkout built" if isinstance(core, Path) else f"{_CORE} {core}"
    failures = 0
    for sample in samples:
        reported = errors[sample.name]
        print(f"{'ok  ' if not reported else 'FAIL'} {sample.name} on {named}")
        for line in reported:
            print(f"       {line}")
        failures += bool(reported)
    return failures, None


def selected(arguments: list[str]) -> list[Path] | str:
    """The samples named on the command line, or all of them; a string is the refusal."""
    every = sample_directories()
    if not every:
        return f"no samples under {_SAMPLES} — this check would pass vacuously"
    if not arguments:
        return every
    by_name = {sample.name: sample for sample in every}
    unknown = [name for name in arguments if Path(name).name not in by_name]
    if unknown:
        return f"no such sample: {', '.join(unknown)}"
    return [by_name[Path(name).name] for name in arguments]


def main(argv: list[str]) -> int:
    """CLI entry: type-check each named sample, or all of them, against its declared core."""
    arguments = argv[1:]
    local_core: Path | None = None
    if "--local-core" in arguments:
        at = arguments.index("--local-core")
        if at + 1 >= len(arguments):
            print(_USAGE.format(program=argv[0]), file=sys.stderr)
            return 2
        local_core = Path(arguments[at + 1]).resolve()
        arguments = arguments[:at] + arguments[at + 2 :]
        if not local_core.is_file():
            print(f"no core wheel at {local_core}", file=sys.stderr)
            return 2

    every = selected(arguments)
    if isinstance(every, str):
        print(every, file=sys.stderr)
        return 2

    groups: dict[tuple[int, ...], list[Path]] = {}
    for sample in every:
        groups.setdefault(core_floor(sample), []).append(sample)

    failures, refusals = 0, 0
    for floor in sorted(groups):
        failed, refused = check_group(floor, groups[floor], local_core)
        failures += failed
        if refused:
            refusals += 1
            print(refused, file=sys.stderr)
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
    raise SystemExit(main(sys.argv))
