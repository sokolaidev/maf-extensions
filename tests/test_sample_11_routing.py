"""The routed sample keeps operations on the spec used for acquisition."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maf_sandbox import Capability, ExecResult, SandboxSpec, ScopePurge


@pytest.mark.parametrize("override", [None, "/files/base"])
def test_act_six_uses_the_acquired_specs_base(monkeypatch, override):
    path = Path(__file__).resolve().parents[1] / "samples/11_router_two_backends/agent.py"
    module_spec = importlib.util.spec_from_file_location("sample_11_routing", path)
    assert module_spec is not None and module_spec.loader is not None
    sample = importlib.util.module_from_spec(module_spec)
    prior_path = sys.path[:]
    try:
        sys.path.insert(0, str(path.parent))
        module_spec.loader.exec_module(sample)
    finally:
        sys.path[:] = prior_path
        for name, loaded in list(sys.modules.items()):
            origin = getattr(loaded, "__file__", None)
            if origin and Path(origin).parent == path.parent:
                del sys.modules[name]

    sandbox = SimpleNamespace(
        write_file=AsyncMock(),
        exec=AsyncMock(return_value=ExecResult(exit_code=0, stdout="routed per spec\n")),
    )
    router = SimpleNamespace(
        backend_for=lambda spec: SimpleNamespace(name="chosen"),
        acquire=AsyncMock(return_value=sandbox),
        dispose_scope=AsyncMock(return_value=ScopePurge(disposed=2)),
    )

    def distinct_specs(**kwargs):
        requires = kwargs.get("requires", frozenset())
        return SandboxSpec(
            **kwargs, work_dir=override if Capability.FILES_OUT in requires else "/plain/base"
        )

    monkeypatch.setattr(sample, "SandboxSpec", distinct_specs)
    monkeypatch.setattr(sample, "backends", lambda: (object(), object()))
    monkeypatch.setattr(sample, "SandboxRouter", lambda *args, **kwargs: router)
    asyncio.run(sample.act_six_the_spec_picks())
    acquired = router.acquire.await_args.args[1]
    assert acquired.work_dir == override
    assert sandbox.write_file.await_args.kwargs["working_directory"] == (override or ".")
    assert sandbox.exec.await_args.kwargs["working_directory"] == (override or ".")
    router.dispose_scope.assert_awaited_once_with(sample.KEY.scope, sample.KEY.thread_id)
