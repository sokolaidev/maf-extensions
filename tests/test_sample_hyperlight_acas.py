"""Environment routing and failure cleanup for the source-only Hyperlight/ACAS sample."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from agent_framework import Content
from maf_sandbox import CallerContext, Cleanup, SandboxRouter
from maf_sandbox.maf import COMPLETED_TEXT

_SAMPLE = (
    Path(__file__).resolve().parent.parent / "samples" / "experimental" / "hyperlight_acas_codeact"
)


@pytest.fixture
def sample(monkeypatch):
    scaffold_spec = importlib.util.spec_from_file_location("_scaffold", _SAMPLE / "_scaffold.py")
    assert scaffold_spec is not None and scaffold_spec.loader is not None
    scaffold = importlib.util.module_from_spec(scaffold_spec)
    scaffold_spec.loader.exec_module(scaffold)
    # Each sample must import its own scaffold; restore the cache after this fixture.
    monkeypatch.setitem(sys.modules, "_scaffold", scaffold)
    spec = importlib.util.spec_from_file_location("hyperlight_acas_sample", _SAMPLE / "agent.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"APP_ENV": "DEV"}, "hyperlight"),
        ({"APP_ENV": " dev "}, "hyperlight"),
        ({"APP_ENV": "CI"}, "hyperlight"),
        ({"CI": "true"}, "hyperlight"),
        ({"CI": "  YES ", "APP_ENV": "PROD"}, "hyperlight"),
        ({"CI": "1", "APP_ENV": "STAGING"}, "hyperlight"),
        ({"CI": "false", "APP_ENV": "PROD"}, "acas"),
        ({"CI": "0", "APP_ENV": "DEV"}, "hyperlight"),
        ({"CI": "no", "APP_ENV": "test"}, "acas"),
        ({"APP_ENV": "STAGING"}, "acas"),
        ({"APP_ENV": "PROD"}, "acas"),
        ({"APP_ENV": "other"}, "acas"),
    ],
)
def test_environment_selection(sample, env, expected):
    assert sample.select_backend(env) == expected


@pytest.mark.parametrize(
    "env", [{}, {"APP_ENV": " "}, {"CI": "false"}, {"CI": "maybe", "APP_ENV": "DEV"}]
)
def test_missing_or_malformed_configuration_is_refused(sample, env):
    with pytest.raises(ValueError):
        sample.select_backend(env)


@pytest.mark.parametrize("name", ["hyperlight", "acas"])
def test_real_backend_accepts_its_codeact_contract_without_acquisition(sample, name):
    env = {
        "ACAS_SANDBOX_ENDPOINT": "https://management.example.invalid",
        "ACAS_SANDBOX_SUBSCRIPTION_ID": "subscription",
        "ACAS_SANDBOX_RESOURCE_GROUP": "group",
        "ACAS_SANDBOX_GROUP": "sandbox-group",
    }
    backend = sample.build_backend(name, env)

    async def no_files(store):
        return []

    try:
        router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
        context = CallerContext(
            current_scope=lambda: "s", current_thread_id=lambda: "t", list_files=no_files
        )
        tools = sample.tools_for(router, name, context)
        assert len(tools) == 1
        assert tools[0].name == "execute_code"
    finally:
        asyncio.run(backend.aclose())


@pytest.mark.parametrize(
    ("platform", "configured", "expected"),
    [
        ("linux", "/sys/fs/cgroup/sample-test", "/sys/fs/cgroup/sample-test"),
        ("linux", "", None),
        ("win32", "/sys/fs/cgroup/sample-test", None),
    ],
)
def test_cgroup_configuration_follows_the_native_host(
    sample, monkeypatch, platform, configured, expected
):
    import maf_sandbox_hyperlight

    monkeypatch.setattr(sample.sys, "platform", platform)
    monkeypatch.setattr(maf_sandbox_hyperlight, "HyperlightSandboxBackend", lambda config: config)
    config = sample.build_backend("hyperlight", {"MAF_HYPERLIGHT_CGROUP_ROOT": configured})
    assert config.linux_cgroup_root == expected


@pytest.fixture
def smoke_stack(sample, monkeypatch):
    monkeypatch.setenv("CI", "true")
    for key in (*sample.SANDBOX_VARS, *sample.MODEL_VARS):
        monkeypatch.delenv(key, raising=False)
    events = []

    async def close():
        events.append("close")

    async def purge(*args):
        events.append("purge")
        return SimpleNamespace(disposed=1, undisposed=None)

    backend = SimpleNamespace(aclose=close)
    router = SimpleNamespace(dispose_scope=AsyncMock(side_effect=purge))
    # Separate items exercise the scaffold's rendering of a structured tool result.
    tool = SimpleNamespace(
        invoke=AsyncMock(
            return_value=[
                Content.from_text(COMPLETED_TEXT),
                Content.from_text("Result: ok"),
                Content.from_text(f"stdout:\n{sample.ANSWER}"),
            ]
        )
    )
    monkeypatch.setattr(sample, "build_backend", lambda name, env: backend)
    monkeypatch.setattr(sample, "SandboxRouter", lambda *args, **kwargs: router)
    monkeypatch.setattr(sample, "tools_for", lambda *args: [tool])
    return SimpleNamespace(events=events, router=router, tool=tool)


def test_smoke_runs_without_azure_configuration(sample, smoke_stack):
    assert asyncio.run(sample.run(smoke=True)) == 0
    smoke_stack.tool.invoke.assert_awaited_once_with(arguments={"code": sample.PROGRAM})
    assert smoke_stack.events == ["purge", "close"]


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Error: unavailable",
        "stdout:\n42",
        "stdout:\n354224848179261915075\n\nexit code: 1",
        "The workload ran to a definitive result.\nResult: ok\nstdout:\n3542248481792619150750",
        "The workload ran to a definitive result.\nResult: failed\nstdout:\n354224848179261915075\nResult: ok\n\nexit code: 1",
        "The workload did not reach a definitive result.\nResult: ok\nstdout:\n354224848179261915075",
    ],
)
def test_wrong_or_failed_tool_output_fails_and_cleans_up(sample, smoke_stack, output):
    smoke_stack.tool.invoke.return_value = output
    with pytest.raises(RuntimeError, match="No successful CodeAct result"):
        asyncio.run(sample.run(smoke=True))
    assert smoke_stack.events == ["purge", "close"]


@pytest.mark.parametrize("error", [RuntimeError("guest unavailable"), asyncio.CancelledError()])
def test_execution_failure_and_cancellation_still_clean_up(sample, smoke_stack, error):
    smoke_stack.tool.invoke.side_effect = error
    with pytest.raises(type(error)):
        asyncio.run(sample.run(smoke=True))
    assert smoke_stack.events == ["purge", "close"]


def test_attachment_failure_still_closes_backend(sample, smoke_stack, monkeypatch):
    def refuse(*args):
        raise RuntimeError("unsupported contract")

    monkeypatch.setattr(sample, "tools_for", refuse)
    with pytest.raises(RuntimeError, match="unsupported contract"):
        asyncio.run(sample.run(smoke=True))
    assert smoke_stack.events == ["purge", "close"]


def test_purge_failure_fails_run_and_still_closes_backend(sample, smoke_stack):
    smoke_stack.router.dispose_scope.side_effect = RuntimeError("purge failed")
    with pytest.raises(RuntimeError, match="purge failed"):
        asyncio.run(sample.run(smoke=True))
    assert smoke_stack.events == ["close"]


def test_unconfirmed_disposal_fails_run_and_still_closes_backend(sample, smoke_stack):
    smoke_stack.router.dispose_scope.side_effect = None
    smoke_stack.router.dispose_scope.return_value = SimpleNamespace(disposed=0, undisposed="worker")
    with pytest.raises(RuntimeError, match="Not fully disposed"):
        asyncio.run(sample.run(smoke=True))
    assert smoke_stack.events == ["close"]


def test_acas_requires_configuration_before_constructing_a_backend(sample, monkeypatch):
    monkeypatch.setenv("APP_ENV", "PROD")
    monkeypatch.setenv("CI", "false")
    for key in sample.SANDBOX_VARS:
        monkeypatch.delenv(key, raising=False)

    def unexpected(*args):
        pytest.fail("created a backend before validating its configuration")

    monkeypatch.setattr(sample, "build_backend", unexpected)
    assert asyncio.run(sample.run(smoke=True)) == 2


def test_scaffold_matches_numbered_samples():
    canonical = _SAMPLE.parents[1] / "03_acas_codeact" / "_scaffold.py"
    assert (_SAMPLE / "_scaffold.py").read_bytes() == canonical.read_bytes()


def test_live_sample_is_wired_only_into_linux_ci():
    workflow = _SAMPLE.parents[2] / ".github" / "workflows" / "tests.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    matching_jobs = [
        job
        for job in jobs.values()
        if any(
            "tests/test_sample_hyperlight_acas_live.py" in step.get("run", "")
            for step in job["steps"]
        )
    ]
    assert len(matching_jobs) == 1
    job = matching_jobs[0]
    assert job["runs-on"].startswith("ubuntu-")
    command = next(
        step["run"]
        for step in job["steps"]
        if "tests/test_sample_hyperlight_acas_live.py" in step.get("run", "")
    )
    assert "scripts/check_hyperlight_linux.py --live" in command
