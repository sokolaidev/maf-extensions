"""Qualification artifacts must exclude provider payloads, including failure details."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    module = importlib.import_module("qualify_record_reasoning")
    instance = module.Probe.__new__(module.Probe)
    instance.provider = "openai"
    instance.model_name = "qualification-model"
    instance.model_version = "qualification-version"
    instance.script_sha256 = "script-digest"
    instance.output = tmp_path
    instance.label = "seed-0"
    instance.requests = []
    instance.responses = []
    instance.events = []
    instance.checks = {}
    instance.http = SimpleNamespace(aclose=AsyncMock())
    instance.credential = SimpleNamespace(close=AsyncMock())
    instance.gemini_sdk = SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock())
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "test-version")
    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kwargs: "test-commit")
    return instance


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("failed", [False, True])
def test_only_sanitized_artifact_is_written(probe, tmp_path, capsys, provider, failed):
    probe.provider = provider
    history_field = {"openai": "input", "anthropic": "messages", "gemini": "contents"}[provider]
    probe.requests = [
        {
            "label": "seed-0",
            "body": {
                history_field: [{"role": "user", "content": "private-prompt-sentinel"}],
                "tools": [{"description": "private-tool-sentinel"}],
            },
        }
    ]
    probe.responses = [
        {
            "label": "seed-0",
            "status": 400 if failed else 200,
            "body": {
                "text": "private-response-sentinel",
                "signature": "private-signature-sentinel",
                "error": "private-error-sentinel",
            },
        }
    ]
    error = RuntimeError("private-exception-sentinel") if failed else None

    asyncio.run(probe.close(error))

    files = list(tmp_path.iterdir())
    assert [path.name for path in files] == [f"{provider}.json"]
    serialized = files[0].read_text(encoding="utf-8")
    assert "private-" not in serialized + capsys.readouterr().out
    report = json.loads(serialized)
    assert report["passed"] is not failed
    assert report["error_type"] == ("RuntimeError" if failed else None)
    assert report["requests"][0]["history_items"] == 1
    assert report["requests"][0]["tool_count"] == 1
    assert len(report["requests"][0]["sha256"]) == 64
    assert len(report["responses"][0]["sha256"]) == 64
    assert report["responses"][0]["http_status"] == (400 if failed else 200)
    probe.http.aclose.assert_awaited_once()
    probe.credential.close.assert_awaited_once()


def test_rejected_transformations_do_not_leak_to_artifacts_or_stdout(probe, tmp_path, capsys):
    module = importlib.import_module("qualify_record_reasoning")
    probe.provider = "anthropic"
    probe.tools = []
    probe.wire = lambda messages: []
    probe.client = SimpleNamespace(
        get_response=AsyncMock(return_value=module.ChatResponse(messages=[]))
    )
    probe.requests = [{"label": "baseline", "body": {"messages": []}}]
    probe.responses = [
        {
            "label": "baseline",
            "status": 200,
            "body": {"input_transformations": [{"detail": "private-transformation-sentinel"}]},
        }
    ]

    with pytest.raises(AssertionError, match="Provider transformed retained thinking") as error:
        asyncio.run(probe.send("baseline", []))
    asyncio.run(probe.close(error.value))

    output = capsys.readouterr().out
    persisted = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert "private-transformation-sentinel" not in output + persisted
    event = json.loads(output)
    assert event["input_transformation_count"] == 1


@pytest.mark.parametrize("failure", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_aborted_probe_never_writes_success(probe, tmp_path, monkeypatch, capsys, failure):
    module = importlib.import_module("qualify_record_reasoning")
    probe.run = AsyncMock(side_effect=failure("private-abort-sentinel"))
    monkeypatch.setattr(module, "Probe", lambda *args: probe)
    monkeypatch.setattr(sys, "argv", ["probe", "--provider", "openai", "--output", str(tmp_path)])
    monkeypatch.setenv("AZURE_OPENAI_MODEL", "model")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")

    with pytest.raises(failure):
        asyncio.run(module.main())

    report_text = (tmp_path / "openai.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["passed"] is False
    assert report["error_type"] == failure.__name__
    assert report["error_stage"] == "seed-0"
    assert "private-abort-sentinel" not in report_text + capsys.readouterr().out
    probe.http.aclose.assert_awaited_once()
    probe.credential.close.assert_awaited_once()


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_report_requires_only_the_selected_provider_sdks(probe, tmp_path, monkeypatch, provider):
    module = importlib.import_module("qualify_record_reasoning")
    probe.provider = provider
    selected = {
        "openai": {"agent-framework-openai", "openai"},
        "anthropic": {"agent-framework-anthropic", "anthropic"},
        "gemini": {"agent-framework-gemini", "google-genai"},
    }[provider]
    installed = {"agent-framework-core", "azure-identity"} | selected

    def version(name):
        if name not in installed:
            raise module.importlib.metadata.PackageNotFoundError(name)
        return "test-version"

    monkeypatch.setattr(module.importlib.metadata, "version", version)
    asyncio.run(probe.close(RuntimeError("private-original-error")))
    report = json.loads((tmp_path / f"{provider}.json").read_text(encoding="utf-8"))
    assert set(report["packages"]) == installed
    assert report["passed"] is False
    assert report["error_type"] == "RuntimeError"
    probe.http.aclose.assert_awaited_once()
    probe.credential.close.assert_awaited_once()
