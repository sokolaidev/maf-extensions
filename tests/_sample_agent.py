"""Load a numbered sample with its own copy of the shared scaffold."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def load_sample(name: str) -> ModuleType:
    directory = _SAMPLES / name
    spec = importlib.util.spec_from_file_location(f"sample_{name}", directory / "agent.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(directory))
        patch.setitem(sys.modules, spec.name, module)
        patch.delitem(sys.modules, "_scaffold", raising=False)
        try:
            spec.loader.exec_module(module)
        finally:
            # Remove the sample's import before the context restores any previous scaffold.
            sys.modules.pop("_scaffold", None)
    return module
