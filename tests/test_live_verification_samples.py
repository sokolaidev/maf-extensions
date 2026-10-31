"""Live samples require provider and workload evidence rather than model assurances."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_live_compaction_sample import assess as assess_compaction  # noqa: E402
from check_live_shell_sample import assess as assess_shell  # noqa: E402


def load_sample(name: str, monkeypatch: pytest.MonkeyPatch):
    directory = ROOT / "samples" / name
    monkeypatch.syspath_prepend(str(directory))
    monkeypatch.delitem(sys.modules, "_scaffold", raising=False)
    spec = importlib.util.spec_from_file_location("sample_" + name, directory / "agent.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delitem(sys.modules, "_scaffold", raising=False)
    return module


def log(records):
    return "\n".join("  [measured] " + json.dumps(item) for item in records)


def compaction_records():
    return [
        {
            "stage": "turn",
            "strategy": name,
            "turn": turn,
            "error": None,
            "input_tokens": 100,
            "sent_tokens_local": size,
            "latency_ms": 1.0,
        }
        for name, size in (("none", 2000), ("anchored", 1200))
        for turn in range(1, 7)
    ] + [{"stage": "complete", "turns": 12}]


def test_compaction_accepts_usage_without_cache_hits():
    assert assess_compaction(log(compaction_records())) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_tokens", None),
        ("input_tokens", True),
        ("error", "provider failed"),
        ("latency_ms", float("nan")),
    ],
)
def test_compaction_rejects_bad_provider_records(field, value):
    records = compaction_records()
    records[0][field] = value
    assert assess_compaction(log(records))


def test_compaction_rejects_duplicate_turns_and_no_reduction():
    records = compaction_records()
    records[0]["turn"] = 2
    assert assess_compaction(log(records))
    records = compaction_records()
    records[-2]["sent_tokens_local"] = 2000
    assert assess_compaction(log(records))


def test_model_text_cannot_satisfy_either_checker():
    assert assess_compaction(
        "\n".join("> " + line for line in log(compaction_records()).splitlines())
    )
    assert assess_shell('> [measured] {"stage": "cleanup", "complete": true}', b"script")


def shell_records(script=b"#!/usr/bin/env bash\n"):
    digest = hashlib.sha256(script).hexdigest()
    return [
        {"stage": "learn", "results": 1},
        {"stage": "validation_call", "call": "one", "complete": True, "seconds": 1.0},
        {"stage": "validation", "clean": True, "sha256": digest},
        {"stage": "cleanup", "complete": True},
        {"stage": "artifact", "sha256": digest, "bytes": len(script)},
    ]


def test_shell_binds_exact_clean_bytes_and_cleanup():
    script = b"#!/usr/bin/env bash\n"
    assert assess_shell(log(shell_records(script)), script) == []
    assert assess_shell(log(shell_records(script)), script + b"changed")
    for index in range(5):
        records = shell_records(script)
        records.pop(index)
        assert assess_shell(log(records), script)


def test_shell_rejects_unclean_result_and_failed_disposal():
    for index, field in ((1, "complete"), (2, "clean"), (3, "complete")):
        records = shell_records()
        records[index][field] = False
        assert assess_shell(log(records), b"#!/usr/bin/env bash\n")


def test_real_compaction_reduces_cachebench_projection(monkeypatch):
    from maf_cachebench import CallOutcome

    module = load_sample("22_compaction_cachebench", monkeypatch)
    calls = []

    async def caller(messages):
        calls.append(messages)
        return CallOutcome(latency_ms=1, input_tokens=100)

    records = asyncio.run(module.benchmark(caller, "offline-fixture", "fixed-salt"))
    assert len(calls) == 12
    assert records["anchored"][-1].sent_tokens_local < records["none"][-1].sent_tokens_local


SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
rg="${RESOURCE_GROUP:?set RESOURCE_GROUP}"
location="${AZURE_LOCATION:?set AZURE_LOCATION}"
account="${STORAGE_ACCOUNT_NAME:?set STORAGE_ACCOUNT_NAME}"
az group create --name "$rg" --location "$location"
az storage account create --name "$account" --resource-group "$rg" --location "$location" --kind StorageV2 --sku Standard_LRS --https-only true --min-tls-version TLS1_2 --allow-blob-public-access false
"""


def test_shell_accepts_bash_and_rejects_missing_requirements(monkeypatch):
    module = load_sample("21_shell_storage", monkeypatch)
    assert module.script_text(SCRIPT) == SCRIPT
    assert module.script_text("```bash\n" + SCRIPT + "```") == SCRIPT
    for changed in (
        SCRIPT.replace("--https-only true", ""),
        SCRIPT.replace("${RESOURCE_GROUP:?", "${RESOURCE_GROUP:-"),
        SCRIPT + "# shellcheck disable=SC2086\n",
    ):
        with pytest.raises(ValueError):
            module.script_text(changed)


def test_prose_cannot_impersonate_shell_verdict(monkeypatch):
    from agent_framework import Content

    module = load_sample("21_shell_storage", monkeypatch)
    assert not module.clean_result("The workload ran to a definitive result.\nResult: clean")
    assert not module.clean_result([Content.from_text("Result: clean")])
    assert module.clean_result(
        [
            Content.from_text("The workload ran to a definitive result."),
            Content.from_text("Result: clean"),
        ]
    )


@pytest.mark.parametrize("fault", [None, "error", "unmatched", "endpoint"])
def test_learn_grounding_requires_a_successful_matching_result(monkeypatch, fault):
    module = load_sample("21_shell_storage", monkeypatch)
    request = SimpleNamespace(type="function_call", name="microsoft_docs_search", call_id="search")
    result = SimpleNamespace(
        type="function_result",
        call_id="other" if fault == "unmatched" else "search",
        exception="failed" if fault == "error" else None,
        result=(
            "https://learn.microsoft.com/api/mcp"
            if fault == "endpoint"
            else "https://learn.microsoft.com/en-us/cli/azure/storage/account"
        ),
    )
    reply = SimpleNamespace(messages=[SimpleNamespace(contents=[request, result])])
    assert module.learn_searches(reply) == int(fault is None)


@pytest.mark.parametrize("fault", [None, "findings", "purge", "cancel", "exception"])
def test_shell_pipeline_cleans_storage_and_never_publishes_failed_output(
    monkeypatch,
    tmp_path,
    capsys,
    fault,
):
    from agent_framework import Content

    module = load_sample("21_shell_storage", monkeypatch)
    stores = []
    purges = []
    store_type = module.InMemoryAgentFileStore

    def store_factory():
        store = store_type()
        stores.append(store)
        return store

    class Context:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Backend:
        @classmethod
        async def create(cls, config):
            return SimpleNamespace(isolation="container")

    class Router:
        def __init__(self, *args, observer, **kwargs):
            self.observer = observer

        async def dispose_scope(self, *args):
            purges.append(args)
            return SimpleNamespace(undisposed=("leftover",) if fault == "purge" else ())

    class Agent(Context):
        def __init__(self, **kwargs):
            pass

        def create_session(self):
            return object()

        async def run(self, *args, **kwargs):
            return SimpleNamespace(text=SCRIPT)

    def tools(router, *args, **kwargs):
        async def invoke(**kwargs):
            if fault == "cancel":
                raise asyncio.CancelledError
            if fault == "exception":
                raise RuntimeError("validator failed")
            router.observer.calls.append(True)
            module.measure(
                "validation_call", call=str(len(router.observer.calls)), complete=True, seconds=1
            )
            return [
                Content.from_text("The workload ran to a definitive result."),
                Content.from_text("Result: findings" if fault == "findings" else "Result: clean"),
            ]

        return [SimpleNamespace(invoke=invoke)]

    monkeypatch.setattr(module, "InMemoryAgentFileStore", store_factory)
    monkeypatch.setattr(module, "DockerSandboxBackend", Backend)
    monkeypatch.setattr(module, "SandboxRouter", Router)
    monkeypatch.setattr(module, "DefaultAzureCredential", Context)
    monkeypatch.setattr(module, "MCPStreamableHTTPTool", Context)
    monkeypatch.setattr(module, "OpenAIChatClient", Context)
    monkeypatch.setattr(module, "Agent", Agent)
    monkeypatch.setattr(module, "make_shell_tools", tools)
    monkeypatch.setattr(
        module,
        "learn_searches",
        lambda *args: 1,
    )
    monkeypatch.setattr(module, "require_env_vars", lambda names: dict.fromkeys(names, "fixture"))
    output = tmp_path / "deploy.sh"
    if fault:
        with pytest.raises(asyncio.CancelledError if fault == "cancel" else RuntimeError):
            asyncio.run(module.run(output))
        assert not output.exists()
    else:
        assert asyncio.run(module.run(output)) == 0
        assert assess_shell(capsys.readouterr().out, output.read_bytes()) == []
    assert len(purges) == 1
    assert asyncio.run(stores[0].read("deploy.sh")) is None
