"""Runtime-device qualification fails closed on missing or unusable devices."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from shell_runtime_devices import DEVICES, check_runtime_devices  # noqa: E402


@pytest.mark.parametrize("failure", [None, "missing", "unusable", "malformed"])
def test_device_probe_requires_success_and_complete_evidence(failure):
    output = json.dumps(list(DEVICES[:-1] if failure == "missing" else DEVICES))
    if failure == "malformed":
        output = "not json"
    execute = AsyncMock(
        return_value=SimpleNamespace(
            exit_code=1 if failure == "unusable" else 0, stdout=output, stderr="device refused"
        )
    )
    sandbox = SimpleNamespace(exec=execute)
    if failure:
        with pytest.raises(ValueError):
            asyncio.run(check_runtime_devices(sandbox))
    else:
        assert asyncio.run(check_runtime_devices(sandbox)) == list(DEVICES)
    assert execute.call_args.kwargs["timeout"] == 30
