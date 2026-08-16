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
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePath

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SAMPLES = _ROOT / "samples"
_SAMPLE_DIRS = sorted(path for path in _SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())

#: PEP 723, as the spec writes it. The same shape `test_sample_metadata.py` parses — that suite
#: owns validating the block against the imports, and this one only reads what it declares.
_BLOCK = re.compile(r"(?m)^# /// script\s*$\s(?P<body>(?:^#(?:| .*)$\s)+)^# ///\s*$")


def test_the_sample_directories_were_found():
    """A glob that matched nothing would make every parametrized case vacuously true."""
    assert len(_SAMPLE_DIRS) >= 10, f"found {len(_SAMPLE_DIRS)} sample directories"


def _declared(sample: Path) -> list[str]:
    """The distribution names a sample's PEP 723 block asks for, version specifiers stripped."""
    match = _BLOCK.search((sample / "agent.py").read_text(encoding="utf-8"))
    assert match, f"{sample.name}/agent.py has no PEP 723 block"
    body = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("body").splitlines(keepends=True)
    )
    return [
        re.match(r"[A-Za-z0-9._-]+", dep).group(0)
        for dep in tomllib.loads(body).get("dependencies", [])
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


def _import(path: Path, sample: Path) -> None:
    """Import ``path`` as a standalone module, with its own directory first on the path.

    `sys.path[0]` is the script's directory when a sample is run, which is what lets
    `from _scaffold import …` and `from host_tools import …` resolve. The same has to hold here
    or a multi-file sample fails for a reason that has nothing to do with the library.

    Every module loaded *from this sample's directory* is then evicted from `sys.modules`, and
    that is the load-bearing half. Thirteen samples carry a module named `_scaffold`: keep the
    cache and the first sample imported answers every later `from _scaffold import …`, so twelve
    are checked against a copy that is not theirs. Only this directory's modules go — the
    framework and the SDKs stay cached, or each sample would pay to import them again.
    """
    spec = importlib.util.spec_from_file_location(f"_sample_{sample.name}_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(sample))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(sample))
        for name, loaded in list(sys.modules.items()):
            origin = getattr(loaded, "__file__", None)
            if origin and Path(origin).parent == sample:
                del sys.modules[name]


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
    """Thirteen samples carry a module named `_scaffold`, and `sys.modules` holds one.

    Cache it and the first sample imported answers every later `from _scaffold import …`, so
    twelve are checked against a copy that is not theirs. Asserted on the cache rather than on a
    drift, because `test_sample_scaffold.py` forbids the drift — which is what would keep this
    hole invisible.
    """
    first, second = _SAMPLE_DIRS[0], _SAMPLE_DIRS[1]
    assert not (_absent(first) or _absent(second)), "these two cannot be imported here"

    saw: list[str | None] = []
    for sample in (first, second):
        _import(sample / "agent.py", sample)
        saw.append(getattr(sys.modules.get("_scaffold"), "__file__", None))

    # Presence as well as absence. Asserting only that the cache ends up empty would pass
    # trivially the day a sample stops importing `_scaffold` at module level, and the eviction
    # could be deleted with nothing going red.
    assert saw == [None, None], f"`_scaffold` outlived the import that loaded it: {saw}"
    assert "_scaffold" in (first / "agent.py").read_text(encoding="utf-8"), (
        f"{first.name} no longer imports `_scaffold`, so the case above proves nothing"
    )


_PYRIGHT = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pyright"]
_SAMPLES_ENV = next(
    (env for env in _PYRIGHT.get("executionEnvironments", []) if env.get("root") == "samples"), {}
)

#: Anything a diagnostic rule can be set to that is weaker than failing. pyright takes a boolean
#: for every rule as well as a severity, so a bare `false` is the shortest way to turn one off.
_WEAKER_THAN_ERROR = ("warning", "information", "none", False)

#: The two rules the probe below trips, because a downgrade can be per-rule: the attribute one
#: is the shape #334 is about, and the assignment one is an ordinary mistake any sample can make.
_PROBE_RULES = {"reportAttributeAccessIssue", "reportAssignmentType"}


def _relaxed(table: dict) -> list[str]:
    """Diagnostic rules in ``table`` set to anything short of failing the build."""
    return sorted(
        key
        for key, value in table.items()
        if key.startswith("report") and value in _WEAKER_THAN_ERROR
    )


def test_pyright_reports_an_error_inside_samples():
    """The only measurement that proves the pass reaches `samples/`. Costs about six seconds.

    Everything else here is a proxy, and each proxy's blind spot was measured rather than
    guessed. `filesAnalyzed` counts a file pyright *opened*: under `ignore = ["./samples"]` it
    opens the file and drops every diagnostic. No reading of the config text sees a rule
    downgraded at the top level of `[tool.pyright]`, which every execution environment inherits.

    So a file with two known errors goes into `samples/`, the whole configured pass runs — the
    invocation CI runs, with no path argument, so `include` counts — and both have to come back.
    """
    probe = _SAMPLES / "_pyright_probe.py"
    probe.write_text(
        '"""Written by tests/test_sample_modules_import.py, deleted in the same call."""\n\n'
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
        f"holding errors under {sorted(_PROBE_RULES)}. samples/ is being silenced — by `exclude`, "
        "by `ignore`, by its removal from `include`, or by a rule downgrade at either level."
    )


def test_pyright_is_configured_to_read_samples():
    """A fast, precise diagnosis for the slow case above, and not a substitute for it.

    An import executes module level and never enters a function body. A removed rung named
    inside `run()` is invisible to every case further up and plain to pyright.
    """
    assert "samples" in _PYRIGHT["include"], f"not in include: {_PYRIGHT['include']}"
    assert _PYRIGHT["typeCheckingMode"] == "standard", (
        f"the root type-checking mode is {_PYRIGHT['typeCheckingMode']!r}; anything weaker than "
        "standard drops rules across scripts/, tests/ and samples/ at once"
    )
    # Compared as path parts, because `./samples`, `samples/**` and `**/samples` all silence the
    # tree and only one of them starts with the word.
    silenced = [
        f"{key}={entry}"
        for key in ("exclude", "ignore")
        for entry in _PYRIGHT.get(key, [])
        if "samples" in PurePath(str(entry)).parts
    ]
    assert not silenced, f"samples/ is named in {silenced}, which overrides `include`"
    assert not _relaxed(_PYRIGHT), (
        f"{_relaxed(_PYRIGHT)} is downgraded at the top level of [tool.pyright], and every "
        "execution environment inherits it — samples/ included, which relaxes nothing of its own"
    )


def test_samples_relax_no_rule():
    """`samples/` answers to what `scripts/` answers to, and this says so out loud.

    Every test tree downgrades four rules for the loose fakes it is made of (#290). The sites in
    `samples/` that would want that are not fakes — they are #370 — and a sample is code an
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
                "problem — and nothing else in this repository would have noticed."
            )
