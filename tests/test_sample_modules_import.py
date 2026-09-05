"""Every sample module still imports — and the two things that make that mean something.

Two mechanisms keep a sample from naming a library attribute that no longer exists, and neither
replaces the other:

* **This suite imports every sample.** A sample's module level is imports, constants and function
  definitions, with the work behind `if __name__ == "__main__"`, so importing executes all of it
  and catches a `FLOOR = Isolation.<a rung that was removed>` at the assignment.
* **pyright reads `samples/`** (#334), which catches the same name inside a function body, where
  no import reaches.

Both are easy to lose quietly, so both are asserted here rather than assumed. The skip is the one
to watch — a suite that skips most of its subjects still reports green — and it is decided
*before* importing, never from the exception an import raised: `from maf_sandbox import Removed`
and a missing `agent-framework-openai` are both `ImportError`, and only the second is a reason to
look away.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from fnmatch import fnmatch
from pathlib import Path, PurePath

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))  # the shared PEP 723 reader lives beside the checks

import sample_blocks  # noqa: E402

_SAMPLES = _ROOT / "samples"
_SAMPLE_DIRS = sorted(path for path in _SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())


def test_the_sample_directories_were_found():
    """A glob that matched nothing would make every parametrized case vacuously true."""
    assert len(_SAMPLE_DIRS) >= 10, f"found {len(_SAMPLE_DIRS)} sample directories"


def _declared(sample: Path) -> list[str]:
    """The distribution names a sample's PEP 723 block asks for, version specifiers stripped."""
    block = sample_blocks.declared(sample / "agent.py")
    assert block is not None, f"{sample.name}/agent.py has no PEP 723 block"
    return [
        name
        for dep in block.get("dependencies", [])
        if (name := sample_blocks.distribution(dep)) is not None
    ]


def _absent(sample: Path) -> list[str]:
    """Which of a sample's declared distributions this workspace does not install.

    Extras are stripped with the version specifier, so `azure-core[aio]` is checked as
    `azure-core`: the distribution is verified and the extra is not.
    """
    missing: list[str] = []
    for dist in _declared(sample):
        try:
            importlib.metadata.distribution(dist)
        except importlib.metadata.PackageNotFoundError:
            missing.append(dist)
    return missing


def _import(path: Path, sample: Path) -> list[str]:
    """Import ``path`` with its own directory first on the path; answer what it loaded from there.

    `sys.path[0]` is the script's directory when a sample is run, which is what lets
    `from _scaffold import …` and `from host_tools import …` resolve. The same has to hold here
    or a multi-file sample fails for a reason that has nothing to do with the library.

    Every module loaded *from this sample's directory* is then evicted from `sys.modules`, and
    that is the load-bearing half. Every sample carries a module named `_scaffold`: keep the
    cache and the first sample imported answers every later `from _scaffold import …`, so all
    the rest are checked against a copy that is not theirs. Only this directory's modules go — the
    framework and the SDKs stay cached, or each sample would pay to import them again.

    The evicted names come back so a caller can assert the load happened rather than infer it
    from the empty cache afterwards, which an unrelated change could make true for free.
    """
    spec = importlib.util.spec_from_file_location(f"_sample_{sample.name}_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Snapshot rather than insert-and-remove: a sample's own `agent.py` puts its directory on
    # the path too, the way `sys.path[0]` does when it runs as a script, and `remove` takes one
    # occurrence of two. The leftover then shadows every later import by path even though the
    # cache below is clean, which is the same defect this eviction exists to prevent.
    before = list(sys.path)
    sys.path.insert(0, str(sample))
    evicted: list[str] = []
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = before
        for name, loaded in list(sys.modules.items()):
            origin = getattr(loaded, "__file__", None)
            if origin and Path(origin).parent == sample:
                del sys.modules[name]
                evicted.append(name)
    return evicted


def test_no_sample_is_skipped_in_a_synced_workspace():
    """A per-case skip reads as coverage in a summary line, and names no subject.

    Failing rather than skipping is the deliberate half: being wrong this way costs one message
    telling a contributor to sync, and being wrong the other way is what #334 is about.
    """
    absent = {sample.name: missing for sample in _SAMPLE_DIRS if (missing := _absent(sample))}
    assert not absent, (
        f"these samples would be skipped, so nothing imports them: {absent}. Every distribution "
        "a sample declares belongs in the root `dev` dependency group or in `packages/`; run "
        "`uv sync` and add anything genuinely missing there rather than letting the case skip."
    )


def test_a_samples_helper_modules_do_not_outlive_its_import():
    """Every sample carries a module named `_scaffold`, and `sys.modules` holds one.

    Cache it and the first sample imported answers every later `from _scaffold import …`, so
    twelve are checked against a copy that is not theirs. Asserted on the cache rather than on a
    drift, because `test_sample_scaffold.py` forbids the drift — which is what would keep this
    hole invisible.
    """
    first, second = _SAMPLE_DIRS[0], _SAMPLE_DIRS[1]
    assert not (_absent(first) or _absent(second)), "these two cannot be imported here"

    for sample in (first, second):
        # Presence and absence in one measurement. Asserting only that the cache ends up empty
        # would pass trivially the day a sample stopped importing `_scaffold` at module level,
        # and the eviction could then be deleted with nothing going red.
        evicted = _import(sample / "agent.py", sample)
        assert "_scaffold" in evicted, (
            f"importing {sample.name} loaded {evicted or 'nothing'} from its own directory, so "
            "it never cached a `_scaffold` for the next sample to inherit — this case is testing "
            "nothing"
        )
        assert "_scaffold" not in sys.modules, (
            f"`_scaffold` stayed cached after importing {sample.name}, so the next sample's "
            "`from _scaffold import …` resolves to this one's copy"
        )


def _posix(spelling: object) -> str:
    """A pyright path setting as one comparable string. `./samples` and `samples` are one path."""
    return PurePath(str(spelling).replace("\\", "/")).as_posix()


def _names_samples(spelling: object) -> bool:
    """Whether a setting names the samples root itself, in any spelling pyright accepts.

    Compared as a path, not as text. A guard that fires on `./samples` — which pyright honours —
    is a defect in the same family as the `startswith` it replaced, pointing the other way.
    """
    return _posix(spelling) == "samples"


_PYRIGHT = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pyright"]
_SAMPLES_ENV = next(
    (env for env in _PYRIGHT.get("executionEnvironments", []) if _names_samples(env.get("root"))),
    {},
)

#: Anything a diagnostic rule can be set to that is weaker than failing. pyright takes a boolean
#: for every rule as well as a severity, so a bare `false` is the shortest way to turn one off.
_WEAKER_THAN_ERROR = ("warning", "information", "none", False)

#: The two rules the probe below trips, because a downgrade can be per-rule: the attribute one
#: is the shape #334 is about, and the assignment one is an ordinary mistake any sample can make.
_PROBE_RULES = {"reportAttributeAccessIssue", "reportAssignmentType"}

#: How old a probe file has to be before it is somebody's abandoned run rather than somebody's
#: live one. The pass takes about six seconds; this is two orders above it.
_ABANDONED_AFTER = 600.0


def _relaxed(table: dict) -> list[str]:
    """Diagnostic rules in ``table`` set to anything short of failing the build."""
    return sorted(
        key
        for key, value in table.items()
        if key.startswith("report") and value in _WEAKER_THAN_ERROR
    )


#: What an `exclude` or `ignore` entry is matched against: the root, every sample directory, and
#: every sample *file*. All three levels silence the tree and only the first is visible to the
#: behavioural probe, which sits at the root where no shipped sample does — `**/agent.py` drops
#: exactly the function bodies #334 exists for.
_SAMPLE_PATHS = [
    _posix(path.relative_to(_ROOT))
    for path in (_SAMPLES, *_SAMPLE_DIRS, *sorted(_SAMPLES.glob("[0-9][0-9]_*/*.py")))
]


def _covers_samples(pattern: str) -> bool:
    """Whether a pyright path pattern reaches the samples root, a sample directory, or a sample.

    `fnmatch` rather than `PurePath.match`, whose `**` is 3.13. Its `*` crosses separators, so
    this errs towards flagging, which is the safe direction for a guard.
    """
    entry = PurePath(pattern.replace("\\", "/"))
    if "samples" in entry.parts or entry == PurePath("."):
        return True
    return any(fnmatch(path, _posix(pattern)) for path in _SAMPLE_PATHS)


def _covering(key: str) -> list[str]:
    """`exclude`/`ignore` entries that reach samples/, whichever way they spell it."""
    return [str(entry) for entry in _PYRIGHT.get(key, []) if _covers_samples(str(entry))]


def _sweep_abandoned_probes(mine: Path) -> None:
    """Remove probe files a killed run left behind, which fail the type gate until they go.

    The `finally` below covers everything short of SIGKILL, and what SIGKILL leaves is a file
    reporting two errors to every `uv run pyright` in the checkout while this suite — which
    filters to its own pid — stays green. Age is the test rather than liveness: a concurrent
    run's probe is seconds old, and nothing portable answers "is that pid still mine".

    Every step is best-effort. Another run sweeping the same file between the glob and the stat,
    or holding it open on Windows, raises out of a sweep whose whole purpose is to survive
    concurrency — and a probe someone else is touching is by definition not abandoned.
    """
    for stale in _SAMPLES.glob("_pyright_probe_*.py"):
        try:
            if stale != mine and time.time() - stale.stat().st_mtime > _ABANDONED_AFTER:
                stale.unlink()
        except OSError:
            continue


def test_pyright_reports_an_error_inside_samples():
    """Measures the pass rather than asking the config about it. Costs about six seconds.

    The proxies below have blind spots that were measured rather than guessed. `filesAnalyzed`
    counts a file pyright *opened*: under `ignore = ["./samples"]` it opens the file and drops
    every diagnostic. No reading of the config text sees a rule downgraded at the top level of
    `[tool.pyright]`, which every execution environment inherits.

    A file with two known errors goes into `samples/`, the whole configured pass runs — the
    invocation CI runs, with no path argument, so `include` counts — and both have to come back.

    What it does *not* prove is the reach into `samples/NN_*/`, where every sample actually
    lives: an `exclude` naming the directories and not the root leaves this probe reporting.
    `test_pyright_is_configured_to_read_samples` matches the patterns against those directories
    and against every sample file, which is where that reach is held.
    """
    # Named for this process. A fixed name is shared state in a checkout this repository expects
    # to be shared: two overlapping runs and the first to finish unlinks the file the second's
    # pyright is still reading, which fails the second with this test's own alarm.
    probe = _SAMPLES / f"_pyright_probe_{os.getpid()}.py"
    _sweep_abandoned_probes(probe)
    probe.write_text(
        '"""Written by tests/test_sample_modules_import.py, deleted in the same call.\n\n'
        "While it exists an independent `uv run pyright` in this checkout reports its two\n"
        'errors. That is the probe, not drift."""\n\n'
        "from maf_sandbox import Isolation\n\n"
        "RUNG = Isolation.NO_SUCH_RUNG\n"
        'COUNT: int = "not an int"\n',
        encoding="utf-8",
        newline="\n",
    )
    try:
        result = subprocess.run(
            ["uv", "run", "pyright", "--outputjson"], cwd=_ROOT, capture_output=True, text=True
        )
        # The exit code says nothing: pyright exits non-zero *because* of the probe. Empty output
        # is the failure worth reporting, and it means the binary never ran.
        if not result.stdout.strip():
            pytest.fail(
                f"pyright produced no output (exit {result.returncode}). Its stderr: "
                f"{result.stderr.strip()[:500] or '(empty)'}"
            )
        reported = {
            diagnostic.get("rule")
            for diagnostic in json.loads(result.stdout)["generalDiagnostics"]
            if probe.name in diagnostic["file"] and diagnostic["severity"] == "error"
        }
    finally:
        probe.unlink(missing_ok=True)

    assert _PROBE_RULES <= reported, (
        f"the configured pyright pass reported {sorted(reported) or 'nothing'} for a sample file "
        f"holding errors under {sorted(_PROBE_RULES)}. samples/ is being silenced somewhere in "
        "[tool.pyright] — `include`, `exclude`, `ignore`, or a rule downgraded at the top level "
        "or in any execution environment whose root covers samples/."
    )


def test_pyright_is_configured_to_read_samples():
    """A fast, precise diagnosis for the slow case above, and not a substitute for it.

    An import executes module level and never enters a function body. A removed rung named
    inside `run()` is invisible to every case further up and plain to pyright.
    """
    assert any(_names_samples(entry) for entry in _PYRIGHT["include"]), (
        f"not in include: {_PYRIGHT['include']}"
    )
    assert _PYRIGHT["typeCheckingMode"] == "standard", (
        f"the root type-checking mode is {_PYRIGHT['typeCheckingMode']!r}; anything weaker than "
        "standard drops rules across scripts/, tests/ and samples/ at once"
    )
    silenced = [f"{key}={entry}" for key in ("exclude", "ignore") for entry in _covering(key)]
    assert not silenced, f"samples/ is named in {silenced}, which overrides `include`"
    assert not _relaxed(_PYRIGHT), (
        f"{_relaxed(_PYRIGHT)} is downgraded at the top level of [tool.pyright], and every "
        "execution environment inherits it — samples/ included, which relaxes nothing of its own"
    )
    # pyright takes the *first* environment whose root covers the file, so one rooted at `.` and
    # listed above the samples entry answers for samples and never reads it.
    ancestors = [
        env.get("root")
        for env in _PYRIGHT.get("executionEnvironments", [])
        if not _names_samples(env.get("root")) and _covers_samples(str(env.get("root", "")))
    ]
    assert not ancestors, (
        f"execution environments rooted at {ancestors} cover samples/, and whichever is listed "
        "first wins — its rule levels answer for samples/ instead of the samples entry's"
    )


def test_samples_relax_no_rule():
    """`samples/` answers to what `scripts/` answers to, and this says so out loud.

    Every test tree downgrades four rules for the loose fakes it is made of (#290). The sites in
    `samples/` that would want that are not fakes — #370 was the example, fixed by making the
    backend satisfy the protocol rather than by relaxing the rule — and a sample is code an
    adopter copies.
    """
    assert _SAMPLES_ENV, "samples lost its pyright execution environment"
    assert "typeCheckingMode" not in _SAMPLES_ENV, (
        f"samples/ sets its own typeCheckingMode ({_SAMPLES_ENV['typeCheckingMode']!r}), which "
        "moves far more than the four rules a test tree relaxes"
    )
    assert not _relaxed(_SAMPLES_ENV), (
        f"samples/ now relaxes {_relaxed(_SAMPLES_ENV)}. Suppress the one site inline with a "
        "`# pyright: ignore[...]` naming its reason instead."
    )


#: A suppression pyright applies to the whole file rather than to one line: `# type: ignore`
#: standing alone, and any `# pyright:` directive that is not a line-level `ignore[...]` —
#: `reportAttributeAccessIssue=false`, `basic`, `strict`.
#: The gap goes *inside* the lookahead. Outside it, `\s*` gives the space back so the lookahead
#: reads " ignore[", does not match, and the sanctioned form is flagged as the forbidden one.
_FILE_WIDE_SUPPRESSION = re.compile(
    r"^\s*#\s*type:\s*ignore\b|#\s*pyright:(?!\s*ignore\[)", re.MULTILINE
)


@pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
def test_no_sample_suppresses_a_rule_for_a_whole_file(sample: Path):
    """The unguarded sibling of the sanctioned inline ignore.

    `reportUnnecessaryTypeIgnoreComment` is what makes `# pyright: ignore[rule]` safe to write in
    a sample: it fails the build once the ignore stops suppressing anything. It does not cover a
    file-level directive. Measured: `# pyright: reportAttributeAccessIssue=false` on line 1 takes
    a `run()`-body error to zero diagnostics, and leaving the comment after fixing the error is
    never flagged — so it silences exactly what #334 exists for and outlives its reason invisibly.

    None exists today. This is what keeps the sanctioned form the only form.
    """
    for path in sorted(sample.glob("*.py")):
        found = _FILE_WIDE_SUPPRESSION.findall(path.read_text(encoding="utf-8"))
        assert not found, (
            f"{sample.name}/{path.name} suppresses a rule for the whole file. Only a line-level "
            "`# pyright: ignore[rule]` is sanctioned here, because only that one fails the build "
            "when it stops suppressing anything."
        )


def test_a_suppression_in_a_sample_cannot_go_stale_unnoticed():
    """The one real objection to suppressing inline, answered in the config.

    A `# pyright: ignore` outlives the thing it was hiding, and nothing would say so. Scoped to
    `samples/` rather than the root, where the package test trees still carry stale ones.
    """
    assert _SAMPLES_ENV.get("reportUnnecessaryTypeIgnoreComment") == "error", (
        "samples/ no longer fails on an unnecessary `# pyright: ignore`, so a suppression that "
        "has outlived its reason stays put and reads as a live one"
    )


@pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
def test_every_module_imports(sample: Path):
    absent = _absent(sample)
    if absent:
        pytest.skip(f"{sample.name} declares {', '.join(absent)}, absent from this workspace")

    for path in sorted(sample.glob("*.py")):
        try:
            _import(path, sample)
        except Exception as exc:  # noqa: BLE001 - every failure here is the point
            pytest.fail(
                f"{sample.name}/{path.name} does not import: {type(exc).__name__}: {exc}. "
                "Its declared dependencies are all installed, so this is the sample's own "
                "problem. pyright would catch a name that no longer exists; only this case "
                "catches module level failing to run."
            )
