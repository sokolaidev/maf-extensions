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
    """
    declared = {
        re.match(r"[A-Za-z0-9._-]+", dep).group(0)
        for dep in _metadata(sample / "agent.py")["dependencies"]
    }
    tree = ast.parse((sample / "agent.py").read_text(encoding="utf-8"))
    imported = {
        _distribution(node.module)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("maf_sandbox")
    } | {
        _distribution(alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("maf_sandbox")
    }
    missing = sorted(imported - declared)
    assert not missing, (
        f"{sample.name}/agent.py imports {', '.join(missing)} and its PEP 723 block does not "
        f"declare it. Declared: {', '.join(sorted(declared))}."
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
