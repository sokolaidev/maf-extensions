"""Keep failure and internal binding projections faithful to their structured outcome."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
qualification = importlib.import_module("test_openclaw_gateway_qualification")


def projection():
    value = qualification.completed_cleanup_projection()
    value["result"]["content"][0]["text"] = "structuredContent:\n" + json.dumps(
        value["result"]["details"]["structuredContent"]
    )
    return value


@pytest.mark.parametrize(
    "mutation",
    [
        "text",
        "duplicate",
        "NaN",
        "Infinity",
        "1e400",
        "number",
        "type",
        "missing",
        "tool",
        "server",
        "method",
        "false-error",
        "numeric-error",
        "null-error",
    ],
)
def test_completed_cleanup_rejects_contradictory_projection(mutation):
    value = projection()
    content = value["result"]["content"]
    if mutation == "text":
        content[0]["text"] = qualification.projected()["result"]["content"][0]["text"]
    elif mutation == "duplicate":
        content[0]["text"] = content[0]["text"].replace(
            '"completed": false', '"completed": true, "completed": false'
        )
    elif mutation in {"NaN", "Infinity", "1e400"}:
        content[0]["text"] = content[0]["text"].replace(
            '"completed": false', f'"extra": {mutation}, "completed": false'
        )
    elif mutation == "number":
        content[0]["text"] = content[0]["text"].replace('"completed": false', '"completed": 0')
    elif mutation == "type":
        content[0]["type"] = "image"
    elif mutation == "missing":
        content[0].pop("text")
    elif mutation.endswith("-error"):
        value["result"]["isError"] = {"false-error": False, "numeric-error": 0, "null-error": None}[
            mutation
        ]
    elif mutation == "tool":
        value["tool"]["id"] = "other"
    elif mutation == "server":
        value["result"]["details"]["mcpServer"] = "other"
    else:
        value["result"]["details"]["mcpTool"] = "other"
    with pytest.raises(RuntimeError):
        qualification.lifecycle.verify_completed_cleanup(
            qualification.completed_cleanup_evidence(), value, "config", "image"
        )


def test_completed_cleanup_accepts_exact_failure_projection():
    assert (
        qualification.lifecycle.verify_completed_cleanup(
            qualification.completed_cleanup_evidence(), projection(), "config", "image"
        )["success_suppressed"]
        is True
    )


@pytest.mark.parametrize("site", ["ownership", "cleanup"])
@pytest.mark.parametrize("mutation", ["text", "duplicate", "number", "error"])
def test_binding_observers_reject_inconsistent_success_before_recording(tmp_path, site, mutation):
    @dataclass(frozen=True)
    class Binding:
        tool: object
        execute: object

    @dataclass(frozen=True)
    class Resource:
        name: str
        cleanup: object

    structured = qualification.projected()["result"]["details"]["structuredContent"]
    text = json.dumps(structured)
    if mutation == "text":
        text = "{}"
    elif mutation == "duplicate":
        text = text.replace('"completed": true', '"completed": false, "completed": true')
    elif mutation == "number":
        text = text.replace('"completed": true', '"completed": 1')
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=structured,
        isError=mutation == "error",
    )

    async def execute(arguments, context):
        return result

    async def cleanup():
        return True

    service = SimpleNamespace(
        bindings={"bicep_validate": Binding(SimpleNamespace(name="bicep_validate"), execute)},
        resources={"bicep-docker": Resource("bicep-docker", cleanup)},
    )
    observer = qualification.observer.ObserveHTTP(
        SimpleNamespace(service=service), tmp_path / "audit"
    )
    if site == "ownership":
        qualification.ownership.observe_binding(observer)
    else:
        fault = tmp_path / "fault"
        fault.touch()
        qualification.observer.refuse_completed_cleanup(observer, fault)
    context = SimpleNamespace(session_id="session", request_id=42)
    with pytest.raises(RuntimeError):
        asyncio.run(service.bindings["bicep_validate"].execute({}, context))
    assert not any(
        row["event"] in {"binding_result", "binding_completed"}
        for row in qualification.checker.records(observer.evidence)
    )


@pytest.mark.parametrize("status", ["ok", "incomplete", "error"])
def test_ownership_binding_observer_preserves_normal_and_diagnostic_results(tmp_path, status):
    @dataclass(frozen=True)
    class Binding:
        tool: object
        execute: object

    structured = qualification.projected()["result"]["details"]["structuredContent"]
    structured.update(
        status=status, completed=status == "ok", verdict="valid" if status == "ok" else None
    )
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(structured))],
        structuredContent=structured,
        isError=status == "error",
    )

    async def execute(arguments, context):
        return result

    service = SimpleNamespace(
        bindings={"bicep_validate": Binding(SimpleNamespace(name="bicep_validate"), execute)}
    )
    observer = qualification.observer.ObserveHTTP(
        SimpleNamespace(service=service), tmp_path / "audit"
    )
    qualification.ownership.observe_binding(observer)
    assert (
        asyncio.run(
            service.bindings["bicep_validate"].execute(
                {}, SimpleNamespace(session_id="session", request_id=42)
            )
        )
        is result
    )
    rows = qualification.checker.records(observer.evidence)
    assert rows[-1]["event"] == "binding_result" and rows[-1]["status"] == status
