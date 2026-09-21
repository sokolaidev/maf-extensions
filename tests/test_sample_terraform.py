"""Sample 20 wires the real kind and attributes disposal to each completed call."""

import asyncio
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_framework import InMemoryAgentFileStore
from maf_sandbox import (
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxDisposed,
    SandboxKey,
    SandboxRouter,
    ToolCallEnded,
)
from maf_sandbox.maf import list_all_files, make_caller_context
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend
from maf_sandbox_terraform import make_terraform_tools
from test_check_live_terraform_sample import check

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples/20_terraform_validation"


@pytest.fixture
def sample(monkeypatch):
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    monkeypatch.setitem(sys.modules, "_scaffold", load("_scaffold", SAMPLE / "_scaffold.py"))
    return load("terraform_sample", SAMPLE / "agent.py")


@pytest.mark.parametrize("engine,version", [("terraform", "1.16.2"), ("opentofu", "1.12.6")])
def test_real_tool_report_and_cleanup_satisfy_the_checker(sample, capsys, engine, version):
    async def exercise():
        phase = {"exit_code": 0, "stdout": "", "stderr": ""}
        validation = {
            "format_version": "1.0",
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "diagnostics": [
                {
                    "severity": "error",
                    "summary": "Missing required argument",
                    "detail": 'The argument "length" is required, but no definition was found.',
                }
            ],
        }
        envelope = {
            "protocol": 1,
            "engine": engine,
            "version": version,
            "error": None,
            "phases": {
                "init": phase,
                "validate": {**phase, "exit_code": 1, "stdout": json.dumps(validation)},
                "fmt": phase,
            },
        }
        sandbox = InProcessSandbox(default_stdout=json.dumps(envelope))
        backend = InProcessSandboxBackend(
            sandbox,
            name="docker",
            isolation=Isolation.CONTAINER,
            declarations=replace(
                FAKE_BACKEND_DECLARATIONS,
                os_families=frozenset({OsFamily.POSIX}),
                isolation_scopes=frozenset({IsolationScope.CALL}),
            ),
        )
        observer = sample.CallCleanup(backend.name)
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER, observer=observer)
        store = InMemoryAgentFileStore()
        await store.write("main.tf", (SAMPLE / "main.tf").read_text("utf-8"))
        context = make_caller_context(
            list_all_files, lambda: sample.SCOPE, lambda: sample.THREAD_ID
        )
        tools = make_terraform_tools(
            router, store, sample.AGENT_DIR, context, engine=engine, image="test:random"
        )
        result = await tools[0].func(files=["main.tf"], root_module=".")
        text = "\n".join(str(item.text) for item in result)
        print(
            sample.evidence(
                f"Diagnostics as {engine}_validate returned them", [text], "validation results"
            )
        )
        assert len(backend.disposed) == 1
        assert observer.complete == [True]
        assert sandbox.commands
        purge = await router.dispose_scope(sample.SCOPE, sample.THREAD_ID)
        print(f"{sample.MEASURED}Disposed {purge.disposed} sandbox(es).")

    asyncio.run(exercise())
    assert (
        check.assess(capsys.readouterr().out, engine=engine, version=version, backend="docker")
        == []
    )


@pytest.mark.parametrize(
    "change", ["missing", "other-call", "other-key", "other-backend", "unknown", "may_remain"]
)
def test_disposal_must_belong_to_this_call_and_every_key(sample, capsys, change):
    observer = sample.CallCleanup("docker")
    key = SandboxKey(scope="samples", thread_id="thread", agent_id="agent")
    disposal = SandboxDisposed(
        key=key, backend="docker", outcome="gone", failure=None, seconds=0.1, call="call"
    )
    if change == "other-call":
        disposal = replace(disposal, call="another")
    elif change == "other-key":
        disposal = replace(disposal, key=replace(key, thread_id="another"))
    elif change == "other-backend":
        disposal = replace(disposal, backend="acas")
    elif change in ("unknown", "may_remain"):
        disposal = replace(disposal, outcome=change)
    if change != "missing":
        observer.sandbox_disposed(disposal)
    observer.tool_call_ended(
        ToolCallEnded(
            tool="terraform_validate",
            kind="terraform",
            keys=(key,),
            seconds=0.2,
            failure=None,
            unclean=0,
            call="call",
        )
    )
    assert observer.complete == [False]
    assert '"disposed": false' in capsys.readouterr().out


@pytest.mark.parametrize("backend", ["docker", "acas"])
@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
@pytest.mark.parametrize("failure", [None, "model", "purge", "cancel"])
def test_sample_selects_engine_and_cleans_up_on_every_exit(
    sample, monkeypatch, backend, engine, failure
):
    for name in (*sample.MODEL_VARS, *sample.SANDBOX_VARS, f"{engine.upper()}_SANDBOX_IMAGE"):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv("SAMPLE_BACKEND", backend)
    monkeypatch.setenv("SAMPLE_ENGINE", engine)
    closed = AsyncMock()
    configured_backend = SimpleNamespace(name=backend, aclose=closed)
    monkeypatch.setattr(sample, "AcasSandboxBackend", lambda config: configured_backend)
    create_docker = AsyncMock(return_value=configured_backend)
    monkeypatch.setattr(sample, "DockerSandboxBackend", SimpleNamespace(create=create_docker))
    credential = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())

    # Special methods are looked up on the type by AsyncExitStack.
    class Credential:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await credential.__aexit__(*args)

    monkeypatch.setattr(sample, "DefaultAzureCredential", Credential)
    purge = AsyncMock(
        return_value=SimpleNamespace(disposed=0, undisposed=None if failure != "purge" else "busy")
    )

    def router(backends, *, min_isolation, observer):
        assert min_isolation == (Isolation.MICROVM if backend == "acas" else Isolation.CONTAINER)
        observer.complete.append(True)
        return SimpleNamespace(dispose_scope=purge)

    monkeypatch.setattr(sample, "SandboxRouter", router)

    def tools(*args, **kwargs):
        assert kwargs == {"engine": engine, "image": "configured"}
        return ["tool"]

    monkeypatch.setattr(sample, "make_terraform_tools", tools)
    monkeypatch.setattr(sample, "OpenAIChatClient", lambda **kwargs: "client")
    error = (
        RuntimeError("model failed")
        if failure == "model"
        else asyncio.CancelledError()
        if failure == "cancel"
        else None
    )
    run = AsyncMock(side_effect=error, return_value=SimpleNamespace(text="reply", messages=[]))
    monkeypatch.setattr(sample, "Agent", lambda **kwargs: SimpleNamespace(run=run))
    if error is not None:
        with pytest.raises(type(error)):
            asyncio.run(sample.run())
    else:
        assert asyncio.run(sample.run()) == (1 if failure == "purge" else 0)
    purge.assert_awaited_once_with(sample.SCOPE, sample.THREAD_ID)
    credential.__aexit__.assert_awaited_once()
    assert closed.await_count == int(backend == "acas")
    assert create_docker.await_count == int(backend == "docker")


@pytest.mark.parametrize(
    "variable,value", [("SAMPLE_ENGINE", "unknown"), ("SAMPLE_BACKEND", "unknown")]
)
def test_invalid_selection_fails_before_configuration(sample, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    assert asyncio.run(sample.run()) == 2
