"""Application checks and lifecycle preservation for the Hyperlight AKS CodeAct sample."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from maf_sandbox import SandboxKey
from maf_sandbox.maf import COMPLETED_TEXT
from maf_sandbox_hyperlight import HyperlightPodConfig
from maf_sandbox_hyperlight.kubernetes import (
    HyperlightPodCleanupPending,
    HyperlightPodController,
    HyperlightPodResult,
    HyperlightPodTemplate,
    pod_manifest,
)

_SAMPLE = Path(__file__).resolve().parent.parent / "samples/experimental/hyperlight_aks_codeact"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample(monkeypatch):
    monkeypatch.setitem(sys.modules, "_scaffold", load("_scaffold", _SAMPLE / "_scaffold.py"))
    return load("aks_sample", _SAMPLE / "agent.py")


@pytest.fixture
def launcher():
    return load("aks_launcher", _SAMPLE / "launch.py")


@pytest.fixture
def binding():
    return HyperlightPodConfig(
        SandboxKey("scope", "thread", "agent"), "codeact", "uid", "gen", 4 * 1024**3
    )


@pytest.fixture
def stack(sample, monkeypatch, binding):
    monkeypatch.setattr(sample.HyperlightPodConfig, "from_environment", lambda: binding)
    events = []
    configs = []

    async def close():
        events.append("close")

    async def purge(*args):
        events.append("purge")
        return SimpleNamespace(disposed=1, undisposed=None)

    def backend(config):
        configs.append(config)
        return SimpleNamespace(aclose=close)

    router = SimpleNamespace(dispose_scope=AsyncMock(side_effect=purge))
    tool = SimpleNamespace(
        invoke=AsyncMock(return_value=f"{COMPLETED_TEXT}\nResult: ok\nstdout:\n{sample.ANSWER}")
    )
    captured = {}

    def attach(router, agent, context, **kwargs):
        captured.update(agent=agent, context=context)
        return [tool]

    monkeypatch.setattr(sample, "HyperlightSandboxBackend", backend)
    monkeypatch.setattr(sample, "SandboxRouter", lambda *args, **kwargs: router)
    monkeypatch.setattr(sample, "make_codeact_tools", attach)
    monkeypatch.setattr(sample.Path, "read_text", lambda *args, **kwargs: "12345")
    return SimpleNamespace(
        events=events, configs=configs, router=router, tool=tool, captured=captured
    )


def test_smoke_uses_binding_and_needs_no_model(sample, stack, monkeypatch, binding, capsys):
    for name in sample.MODEL_VARS:
        monkeypatch.delenv(name, raising=False)
    assert asyncio.run(sample.run()) == 0
    assert stack.configs[0].pod is binding
    assert stack.configs[0].max_worker_memory_bytes is None
    assert stack.captured["agent"] == binding.key.agent_id
    assert stack.captured["context"].current_scope() == binding.key.scope
    assert stack.captured["context"].current_thread_id() == binding.key.thread_id
    stack.tool.invoke.assert_awaited_once_with(arguments={"code": sample.PROGRAM})
    stack.router.dispose_scope.assert_awaited_once_with("scope", "thread")
    assert stack.events == ["purge", "close"]
    assert '"complete": true' in capsys.readouterr().out


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Result: ok\nstdout:\n42",
        f"{COMPLETED_TEXT}\nResult: ok\nstdout:\n3542248481792619150750",
        "The workload did not reach a definitive result.\nResult: ok\nstdout:\n354224848179261915075",
    ],
)
def test_wrong_or_incomplete_output_fails_and_cleans_up(sample, stack, output, capsys):
    stack.tool.invoke.return_value = output
    with pytest.raises(RuntimeError, match="No successful CodeAct"):
        asyncio.run(sample.run())
    assert stack.events == ["purge", "close"]
    assert '"complete": true' not in capsys.readouterr().out


@pytest.mark.parametrize("error", [RuntimeError("worker failed"), asyncio.CancelledError()])
def test_execution_failure_cleans_up(sample, stack, error):
    stack.tool.invoke.side_effect = error
    with pytest.raises(type(error)):
        asyncio.run(sample.run())
    assert stack.events == ["purge", "close"]


def test_incomplete_purge_still_closes_and_never_reports_success(sample, stack, capsys):
    stack.router.dispose_scope.side_effect = None
    stack.router.dispose_scope.return_value = SimpleNamespace(disposed=0, undisposed="pending")
    with pytest.raises(RuntimeError, match="Not fully disposed"):
        asyncio.run(sample.run())
    assert stack.events == ["close"]
    assert '"complete": true' not in capsys.readouterr().out


def test_no_supervisor_binding_refuses_before_backend(sample, monkeypatch):
    def missing():
        raise FileNotFoundError("no supervisor binding")

    monkeypatch.setattr(sample.HyperlightPodConfig, "from_environment", missing)
    with pytest.raises(FileNotFoundError):
        asyncio.run(sample.run())


def test_model_configuration_refused_before_allocation(sample, stack, monkeypatch):
    for name in sample.MODEL_VARS:
        monkeypatch.delenv(name, raising=False)
    assert asyncio.run(sample.run(smoke=False)) == 2
    for name, value in zip(
        sample.MODEL_VARS, ["http://example.invalid/v1", "model", "test-key"], strict=True
    ):
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="HTTPS"):
        asyncio.run(sample.run(smoke=False))
    assert stack.configs == []


@pytest.mark.parametrize("secret", [None, "model-config"])
def test_secret_references_preserve_pod_contract(launcher, monkeypatch, binding, secret):
    sent = []
    monkeypatch.setattr(
        HyperlightPodController, "api", lambda self, *args, body=None: sent.append(body) or {}
    )
    controller = launcher.SampleController(
        kubeconfig="config", context="context", namespace="apps", model_secret=secret
    )
    manifest = pod_manifest(
        binding.key,
        "codeact",
        HyperlightPodTemplate("example.invalid/image@sha256:" + "a" * 64, ("python", "agent.py")),
        namespace="apps",
        generation="g",
        secret_digest="b" * 64,
    )
    manifest = cast("dict[str, Any]", manifest)
    original_env = list(manifest["spec"]["containers"][0]["env"])
    controller.api("create", "-f", "-", body=manifest)
    container = sent[0]["spec"]["containers"][0]
    assert container["env"][: len(original_env)] == original_env
    assert manifest["spec"]["containers"][0]["env"] == original_env
    additions = container["env"][len(original_env) :]
    assert additions == (
        []
        if secret is None
        else [
            {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": name}}}
            for name in launcher.MODEL_VARS
        ]
    )
    assert sent[0]["spec"]["automountServiceAccountToken"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    ledger = {"kind": "ConfigMap", "data": {"state": "reserved"}}
    controller.api("create", "-f", "-", body=ledger)
    assert sent[-1] == ledger


def test_pending_cleanup_and_recovery_use_same_scope(launcher, monkeypatch):
    args = [
        "--kubeconfig",
        "config",
        "--context",
        "ctx",
        "--namespace",
        "apps",
        "--scope",
        "s",
        "--thread",
        "t",
        "--agent",
        "a",
    ]
    keys = []

    def pending(self, key, kind, template):
        keys.append((key, kind))
        raise HyperlightPodCleanupPending("pending")

    def recover(self, key, kind, *, retire):
        keys.append((key, kind))
        assert retire is True
        from maf_sandbox_hyperlight.kubernetes import HyperlightPodExit

        return HyperlightPodExit(70, "retired")

    monkeypatch.setattr(launcher.SampleController, "supervise", pending)
    assert launcher.main(["run", *args, "--image", "example.invalid/image@sha256:" + "a" * 64]) == 3
    monkeypatch.setattr(launcher.SampleController, "recover_exit", recover)
    assert launcher.main(["recover", *args]) == 1
    assert keys == [(SandboxKey("s", "t", "a"), "codeact")] * 2


def test_launcher_selects_model_explicitly_and_propagates_exit(launcher, monkeypatch):
    templates = []

    def supervise(self, key, kind, template):
        templates.append(template)
        return HyperlightPodResult("uid", 0, 1.0, "")

    monkeypatch.setattr(launcher.SampleController, "supervise", supervise)
    args = [
        "run",
        "--kubeconfig",
        "config",
        "--context",
        "ctx",
        "--namespace",
        "apps",
        "--scope",
        "s",
        "--thread",
        "t",
        "--image",
        "example.invalid/image@sha256:" + "a" * 64,
    ]
    assert launcher.main(args) == 0
    assert "--model" not in templates[-1].command
    assert launcher.main([*args, "--model-secret", "model-config"]) == 0
    assert templates[-1].command[-1] == "--model"


@pytest.mark.parametrize(
    "has_result,reply",
    [(True, ANSWER) for ANSWER in ["354224848179261915075", "42"]]
    + [(False, "354224848179261915075")],
)
def test_model_requires_tool_evidence_and_matching_answer(
    sample, stack, monkeypatch, has_result, reply
):
    import agent_framework
    import agent_framework.openai

    for name, value in zip(
        sample.MODEL_VARS, ["https://example.invalid/v1", "test-model", "test-key"], strict=True
    ):
        monkeypatch.setenv(name, value)
    contents = (
        [
            SimpleNamespace(type="function_call", name="execute_code", call_id="call-1"),
            SimpleNamespace(
                type="function_result", call_id="call-1", result=stack.tool.invoke.return_value
            ),
        ]
        if has_result
        else []
    )
    response = SimpleNamespace(text=reply, messages=[SimpleNamespace(contents=contents)])
    turn = AsyncMock(return_value=response)
    client_args = []
    monkeypatch.setattr(
        agent_framework.openai,
        "OpenAIChatClient",
        lambda **kwargs: client_args.append(kwargs) or object(),
    )
    monkeypatch.setattr(agent_framework, "Agent", lambda **kwargs: SimpleNamespace(run=turn))
    if has_result and reply == sample.ANSWER:
        assert asyncio.run(sample.run(smoke=False)) == 0
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(sample.run(smoke=False))
    assert client_args == [
        {"model": "test-model", "base_url": "https://example.invalid/v1", "api_key": "test-key"}
    ]
    turn.assert_awaited_once_with(sample.TASK)
    assert stack.events == ["purge", "close"]


def test_real_pod_backend_attaches_codeact_without_acquiring(sample, binding):
    from maf_sandbox import Cleanup, SandboxRouter
    from maf_sandbox.maf import list_no_files, make_caller_context
    from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools

    backend = sample.HyperlightSandboxBackend(
        sample.HyperlightSandboxConfig(pod=binding, max_worker_memory_bytes=None)
    )
    try:
        router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
        context = make_caller_context(
            list_no_files, lambda: binding.key.scope, lambda: binding.key.thread_id
        )
        tools = make_codeact_tools(
            router,
            binding.key.agent_id,
            context,
            runtime=CodeactRuntime(sample.RUNTIME_INSTRUCTIONS),
        )
        assert len(tools) == 1 and tools[0].name == "execute_code"
    finally:
        asyncio.run(backend.aclose())


@pytest.mark.parametrize(
    "error", [FileNotFoundError("missing"), PermissionError("denied"), ValueError("invalid")]
)
def test_unavailable_memory_metric_preserves_success(sample, stack, monkeypatch, error, capsys):
    def unreadable(*args, **kwargs):
        raise error

    monkeypatch.setattr(sample.Path, "read_text", unreadable)
    assert asyncio.run(sample.run()) == 0
    assert stack.events == ["purge", "close"]
    assert '"memory_peak_bytes": null' in capsys.readouterr().out


def test_model_secret_replaces_existing_entries_once(launcher, monkeypatch, binding):
    sent = []
    monkeypatch.setattr(
        HyperlightPodController, "api", lambda self, *args, body=None: sent.append(body) or {}
    )
    controller = launcher.SampleController(
        kubeconfig="config", context="context", namespace="apps", model_secret="approved-model"
    )
    manifest = cast(
        "dict[str, Any]",
        pod_manifest(
            binding.key,
            "codeact",
            HyperlightPodTemplate(
                "example.invalid/image@sha256:" + "a" * 64, ("python", "agent.py")
            ),
            namespace="apps",
            generation="g",
            secret_digest="b" * 64,
        ),
    )
    environment = manifest["spec"]["containers"][0]["env"]
    environment.extend({"name": name, "value": "old"} for name in launcher.MODEL_VARS)
    environment.append({"name": "OPENAI_MODEL", "value": "duplicate"})
    original = list(environment)
    controller.api("create", "-f", "-", body=manifest)
    result = sent[0]["spec"]["containers"][0]["env"]
    assert [entry for entry in result if entry["name"] not in launcher.MODEL_VARS] == [
        entry for entry in original if entry["name"] not in launcher.MODEL_VARS
    ]
    for name in launcher.MODEL_VARS:
        assert [entry for entry in result if entry["name"] == name] == [
            {"name": name, "valueFrom": {"secretKeyRef": {"name": "approved-model", "key": name}}}
        ]
    assert environment == original
