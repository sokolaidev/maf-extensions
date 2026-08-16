"""Every sample module still imports — and the two things that make that mean something.

A sample naming a library attribute that no longer exists used to be caught by nothing offline.
It went green through the whole gate and raised the first time the sample ran, which is
`verify-live`, after a release, on the workflow that creates billable sandboxes. Two mechanisms
answer that now and neither replaces the other:

* **This suite imports every sample.** A sample's module level is imports, constants and
  function definitions, with the work behind `if __name__ == "__main__"`, so importing walks all
  of it and *executes* it — which catches a `FLOOR = Isolation.PROCESS` at the assignment.
* **pyright reads `samples/`** (#334), which catches the same name inside a function body, where
  no import reaches.

Both are load-bearing and both are easy to lose quietly, so both are asserted below. The skip is
the one to watch: it is decided *before* importing, never from the exception an import raised —
`from maf_sandbox import Removed` and a missing `agent-framework-openai` are both `ImportError`
and only the second is a reason to look away — and for a while it looked away from ten of the
thirteen samples, every one that calls a model. A suite that skips most of its subjects reports
green and covers nothing.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import sys
import tomllib
from pathlib import Path

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
    """Which of a sample's declared distributions this workspace does not install."""
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
    """
    spec = importlib.util.spec_from_file_location(f"_sample_{sample.name}_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(sample))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(sample))


def test_no_sample_is_skipped_in_a_synced_workspace():
    """The skip below is a hole, so it is measured rather than trusted.

    Every distribution a sample declares is either a workspace member or in the root dev group,
    so a synced workspace skips nothing. Ten of the thirteen skipped until `agent-framework-
    openai` was added there, and the suite reported green throughout — a per-case skip is
    invisible in a `-q` run and reads as coverage in a summary line.

    Failing here rather than skipping is deliberate. The cost of being wrong in this direction is
    one clear message telling a contributor to sync; the cost of the other is what #334 is about.
    """
    absent = {sample.name: _absent(sample) for sample in _SAMPLE_DIRS if _absent(sample)}
    assert not absent, (
        f"these samples would be skipped, so nothing imports them: {absent}. Every distribution "
        "a sample declares belongs in the root `dev` dependency group or in `packages/`. Run "
        "`uv sync --all-packages`, and if something is genuinely missing, add it there rather "
        "than letting the case skip."
    )


def _pyright() -> dict:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pyright"]


def test_pyright_is_configured_to_read_samples():
    """The other half of the pair, which no import can stand in for (#334).

    An import executes module level; it never enters a function body. `Isolation.PROCESS` inside
    `run()` is invisible to every case above and plain to pyright, and dropping `samples` from
    the include would take that away with nothing going red — the change is one word in a config
    file and the gap it reopens is only visible on a live run.
    """
    include = _pyright()["include"]
    assert "samples" in include, (
        f"samples is no longer in [tool.pyright] include, so nothing type-checks a sample: {include}"
    )


def test_samples_are_checked_at_the_same_strength_as_scripts():
    """`samples/` relaxes nothing, and this is what says so out loud.

    Every test tree downgrades four rules to warnings for the loose fakes they are made of
    (#290). Extending that to `samples/` was #334's proposal and is not what shipped: the five
    sites needing it are not fakes — two are a real protocol defect in `maf-sandbox-wslc` (#370)
    and three an Optional the library genuinely returns — and downgrading four rules across 23
    files to cover five lines would take a real wrong-argument bug in a sample down with it.

    A relaxation added here would be one line in a config, invisible in a diff summary, and the
    samples pass would keep reporting green while quietly checking less.
    """
    envs = {env.get("root"): env for env in _pyright().get("executionEnvironments", [])}
    assert "samples" in envs, "samples lost its pyright execution environment"
    relaxed = sorted(
        k for k, v in envs["samples"].items() if v in ("warning", "information", "none")
    )
    assert not relaxed, (
        f"samples/ now relaxes {relaxed}. Those rules are what catch a sample naming an attribute "
        "another package deleted, or passing the wrong type to a helper. Suppress the one site "
        "inline with a `# pyright: ignore[...]` naming its reason instead."
    )


def test_a_suppression_in_a_sample_cannot_go_stale_unnoticed():
    """The one real objection to suppressing inline, answered in the config.

    A `# pyright: ignore` outlives the thing it was hiding — #370's two come out when that is
    fixed, and nothing would say so. The rule below fails the build on a suppression that has
    stopped suppressing anything. It is scoped to `samples/` rather than set at the root because
    the root has 27 stale ones already, across six packages' test trees: a separate job.
    """
    envs = {env.get("root"): env for env in _pyright().get("executionEnvironments", [])}
    assert envs.get("samples", {}).get("reportUnnecessaryTypeIgnoreComment") == "error", (
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
