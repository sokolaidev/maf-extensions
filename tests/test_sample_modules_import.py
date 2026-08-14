"""Every sample module still imports — the check nothing else in this repository makes.

`samples/` is outside `[tool.pyright]`'s `include`, and no suite imports a sample's `agent.py`,
so a sample referring to an attribute the library no longer has is caught by nothing offline.
It goes green through the whole gate and raises the first time the sample actually runs, which
is on the live path, after a release.

That is not hypothetical. `Isolation.PROCESS` was renamed to `NONE` in #331, and
`samples/11_router_two_backends` kept the old spelling through a full green run — `1583 passed`,
ruff clean, pyright 0 errors — because importing it is the only thing that would have looked.

Importing is the whole test. Every sample's module-level code is imports, constants and
function definitions, with the work behind `if __name__ == "__main__"`, so an import executes
exactly the lines that name library attributes and nothing that costs anything.

A sample whose dependencies this workspace does not install is skipped by name rather than
passed. Most samples need `agent_framework`, which is not a workspace member; the ones reachable
today are those importing only `maf_sandbox*`. Skipping keeps the suite honest about its own
coverage instead of quietly reporting a pass it did not earn.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_SAMPLE_DIRS = sorted(path for path in _SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())


def test_the_sample_directories_were_found():
    """A glob that matched nothing would make every parametrized case vacuously true."""
    assert len(_SAMPLE_DIRS) >= 10, f"found {len(_SAMPLE_DIRS)} sample directories"


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
    entry_point_reached = False
    unreachable: list[str] = []
    for path in sorted(sample.glob("*.py")):
        try:
            _import(path, sample)
        except ImportError as exc:
            # A dependency this workspace does not install — `agent_framework` and friends.
            # Recorded per module rather than abandoning the sample, or one unreachable
            # `agent.py` would take its importable siblings with it: `09_inprocess_bicep` keeps
            # its backend in `no_isolation_backend.py`, which needs nothing outside the
            # workspace and is checked here whatever `agent.py` does.
            unreachable.append(f"{path.name} needs {exc.name or exc}")
        except Exception as exc:  # noqa: BLE001 - the point is to surface whatever it was
            pytest.fail(
                f"{sample.name}/{path.name} does not import: {type(exc).__name__}: {exc}. "
                "A sample that cannot be imported cannot be run, and nothing else in this "
                "repository would have noticed."
            )
        else:
            entry_point_reached |= path.name == "agent.py"

    # Every sample carries `_scaffold.py`, which imports anywhere. Reporting a pass on that
    # while `agent.py` — the module that names the library — went unimported would be the
    # coverage illusion this suite exists to avoid, so the verdict follows the entry point.
    if not entry_point_reached:
        pytest.skip(f"{sample.name}/agent.py unreachable here — {'; '.join(unreachable)}")
