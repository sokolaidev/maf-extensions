"""Every sample declares its own dependencies, and they have to match what it imports.

`agent.py` carries a PEP 723 `# /// script` block, which is what `uv run agent.py` reads. That
replaced a `pip install` line in each README — prose nothing could check, duplicated into
`verify-live.yml`, and free to drift from the imports it was supposed to describe.

Declaring them in the file does not make them correct; it makes them *checkable*. This is the
check: every `maf_sandbox*` module a sample imports must come from a distribution the block
names, and the block must parse as the TOML the spec says it is.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_SAMPLE_DIRS = sorted(path for path in _SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())

#: PEP 723, as the spec writes it: a `script` block of `#`-prefixed lines.
_BLOCK = re.compile(
    r"(?m)^# /// script\s*$\s(?P<body>(?:^#(?:| .*)$\s)+)^# ///\s*$",
)


#: The module a distribution installs. Underscores in, hyphens out, and `maf_sandbox` itself
#: is the one that is not a suffix of anything.
def _distribution(module: str) -> str:
    return module.split(".")[0].replace("_", "-")


def _metadata(agent: Path) -> dict:
    match = _BLOCK.search(agent.read_text(encoding="utf-8"))
    assert match, (
        f"{agent.parent.name}/agent.py has no PEP 723 block. `uv run agent.py` — which is what "
        "its README says to run — has nothing to resolve without one."
    )
    body = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("body").splitlines(keepends=True)
    )
    return tomllib.loads(body)


@pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
def test_the_block_parses_and_names_a_python_floor(sample: Path):
    metadata = _metadata(sample / "agent.py")
    assert metadata.get("requires-python"), "the block declares no requires-python"
    assert metadata.get("dependencies"), "the block declares no dependencies"


@pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
def test_every_library_import_is_declared(sample: Path):
    """The check prose could not do.

    A sample that grows an import of another kind, and forgets the block, resolves fine in this
    workspace — every sibling is already on the path — and fails for the reader who follows the
    README. That is the exact failure the samples exist to not have.

    **Every** `.py` in the directory, not just `agent.py`. `uv run agent.py` builds one
    environment for whatever that run imports, so a module beside it — `diagram_kind.py`,
    `_scaffold.py` — is resolved from the same block and gets no say in it. Checking only the
    entry point would have gone blind the moment a sample grew a second file.
    """
    declared = {
        re.match(r"[A-Za-z0-9._-]+", dep).group(0)
        for dep in _metadata(sample / "agent.py")["dependencies"]
    }
    imported: dict[str, str] = {}
    for module in sorted(sample.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("maf_sandbox"):
                    imported.setdefault(_distribution(node.module), module.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("maf_sandbox"):
                        imported.setdefault(_distribution(alias.name), module.name)
    missing = sorted(set(imported) - declared)
    assert not missing, (
        f"{sample.name} imports {', '.join(f'{d} (in {imported[d]})' for d in missing)} and its "
        f"PEP 723 block does not declare it. Declared: {', '.join(sorted(declared))}."
    )


class TestTheDeclaredCoreFloor:
    """A floor is a claim, and until #343 nothing read it.

    `uv run --no-project` resolves the **newest** admissible version, so a floor that is too
    low is never exercised: the sample runs against current core, passes its live check, and
    the false claim sits in the file indefinitely. It bites only a consumer whose resolution
    is capped elsewhere — and it does not always bite loudly. Three samples declared `>=0.12`
    while depending on the `work_dir` default that moved in 0.13; resolved at their own floor,
    the source lands in `/work` while `bicepconfig.json` sits in `/maf-sandbox/work`, the
    compiler never finds it, and the model reports diagnostics produced without the configured
    rules. Nothing raises.

    Nothing short of *running* a sample at its declared floor can prove the floor honest, and
    that needs a live model and a real backend per version. So the claim is made true by
    construction instead: a sample is documentation of the current library, not a package with
    consumers to keep compatible, so every sample declares the current core minor and these
    tests are what stop that rotting. `scripts/set_dependents_range.py` moves them all after a
    core release, in the pull request that already moves the packages' range.
    """

    _CORE = Path(__file__).resolve().parent.parent / "packages" / "maf-sandbox" / "pyproject.toml"
    #: A pre-1.0 release of this project, in a comment. `3.12` and the like are left alone.
    _CORE_RELEASE = re.compile(r"0\.\d+(?:\.\d+)?")

    def _core_minor(self) -> tuple[int, int]:
        version = tomllib.loads(self._CORE.read_text(encoding="utf-8"))["project"]["version"]
        major, minor = version.split(".")[:2]
        return int(major), int(minor)

    def _floor(self, sample: Path) -> tuple[int, ...] | None:
        for dep in _metadata(sample / "agent.py")["dependencies"]:
            if re.match(r"[A-Za-z0-9._-]+", dep).group(0) != "maf-sandbox":
                continue
            match = re.fullmatch(r"maf-sandbox>=(\d+(?:\.\d+)*)", dep.strip())
            assert match, (
                f"{sample.name} declares {dep!r}. The floor has to be a bare "
                "`maf-sandbox>=X` — that is the shape scripts/set_dependents_range.py edits "
                "after a core release, and one it cannot read stops that step."
            )
            return tuple(int(part) for part in match.group(1).split("."))
        return None

    @pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
    def test_every_sample_declares_one(self, sample: Path):
        assert self._floor(sample) is not None, (
            f"{sample.name} names no maf-sandbox floor. Every sample imports the core, and "
            "taking whatever a backend distribution happens to pull in is how a sample comes "
            "to run on a version it was never checked against."
        )

    def test_they_all_declare_the_same_one(self):
        floors = {sample.name: self._floor(sample) for sample in _SAMPLE_DIRS}
        distinct = sorted({floor for floor in floors.values() if floor is not None})
        assert len(distinct) == 1, (
            "the samples declare different floors: "
            + ", ".join(f"{name}={floor}" for name, floor in sorted(floors.items()))
            + ". A per-sample floor is a per-sample claim, and each one is a claim nothing "
            "runs. They move together or they drift apart."
        )

    def test_it_is_the_current_core_minor(self):
        """One minor behind is tolerated, and only because of the release window.

        Not slack. The core's version in `pyproject.toml` moves when the Release PR merges,
        and the wheel reaches PyPI later — the publish run is held at "Approve and run".
        Bumping the samples in that window would point them at a version nobody can install,
        which AGENTS.md forbids outright, so the range pull request that opens *after* the
        upload is what moves them. One minor is the whole window: RELEASING.md's release order
        will not let a second core minor out until that pull request has merged and published.
        """
        core = self._core_minor()
        floors = {floor[:2] for floor in map(self._floor, _SAMPLE_DIRS) if floor is not None}
        behind = sorted(floor for floor in floors if floor < (core[0], core[1] - 1))
        ahead = sorted(floor for floor in floors if floor > core)
        assert not ahead, (
            f"a sample declares {ahead[0]} and this repository's core is at {core}. "
            "AGENTS.md: a dependency floor may only name a version that exists."
        )
        assert not behind, (
            f"a sample declares {behind[0]} and this repository's core is at {core}. Move every "
            "sample's floor with `python scripts/set_dependents_range.py <released-version>`."
        )

    @pytest.mark.parametrize("sample", _SAMPLE_DIRS, ids=lambda path: path.name)
    def test_no_comment_beside_it_names_a_release(self, sample: Path):
        """The same trap `tests/test_release_config.py` closes for the packages.

        The bump script rewrites the constraint and never the prose above it, so a sentence
        naming a version is stale one release later. Sample 09 carried "0.14 for
        Isolation.NONE" beside its floor; nothing was going to notice when the floor moved on
        without it. Under a uniform floor there is nothing left for such a comment to justify.
        """
        match = _BLOCK.search((sample / "agent.py").read_text(encoding="utf-8"))
        assert match
        for line in match.group("body").splitlines():
            body = line[1:].strip()
            if not body.startswith("#"):
                continue
            named = self._CORE_RELEASE.search(body)
            assert not named, (
                f"{sample.name}: {body!r} names {named.group(0)}. The floor below it is the "
                "source of truth and it moves without this comment — say what the dependency "
                "is for, not which release it points at."
            )


def test_no_readme_still_tells_a_reader_to_pip_install():
    """The block is the single source now; a README recipe beside it is a second one."""
    offenders = [
        path.relative_to(_SAMPLES).as_posix()
        for path in _SAMPLES.rglob("README.md")
        if "pip install" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"these still carry a pip recipe: {', '.join(offenders)}. The dependencies live in the "
        "`# /// script` block, and a second list is the drift this replaced."
    )
