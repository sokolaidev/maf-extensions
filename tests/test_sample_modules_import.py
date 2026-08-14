"""Every sample module still imports.

`samples/` is outside `[tool.pyright]`'s `include` and no other suite imports a sample, so a
sample naming a library attribute that no longer exists is caught by nothing offline — it goes
green through the whole gate and raises the first time the sample runs, on the live path after
a release. Importing is what looks: a sample's module level is imports, constants and function
definitions, with the work behind `if __name__ == "__main__"`.

A sample is skipped only when a distribution its own PEP 723 block declares is absent from this
workspace. Whether to skip is therefore decided *before* importing, never from the exception an
import raised: `from maf_sandbox import Removed` and a missing `agent-framework-openai` are both
`ImportError`, and only the second is a reason to look away.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
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
