"""Recovery acceptance requires a real failed call and an unchanged same-turn retry."""

from __future__ import annotations

import argparse
import copy
import json
from typing import Any

import pytest
from test_openclaw_e2e_sample import CONFIG, IMAGE, TURN, agent, messages, record
from test_openclaw_hosted_report import model_sample, report

recovery_accepted = report.recovery_accepted

module = agent.WorkerRestart.__init__.__globals__
TOOL = "bicep__bicep_validate"


def stream(call_id: str, *, source: str | None = None) -> bytes:
    args = {
        "id": TOOL,
        "args": {"files": [{"path": "main.bicep", "content": source or agent.INVALID}]},
    }
    encoded = json.dumps(args)
    chunks = []
    for index, part in enumerate((encoded[:15], encoded[15:])):
        call: dict[str, Any] = {"index": 0, "function": {"arguments": part}}
        if index == 0:
            call.update(id=call_id)
            call["function"]["name"] = "dispatch_action"
        chunks.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [call]}}]}))
    return ("\n\n".join(chunks) + "\n\ndata: [DONE]\n\n").encode()


def user(turn: str = TURN) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": "Validate. sample-turn=" + turn}]}


def failure() -> dict[str, Any]:
    body = user()
    body["messages"].append(
        {
            "role": "tool",
            "tool_call_id": "failed",
            "content": json.dumps({"error": {"message": "Worker unavailable"}}),
        }
    )
    return body


def controller() -> tuple[Any, list[str]]:
    operations = []
    return agent.WorkerRestart(
        lambda: operations.append("stop"), lambda: operations.append("restart")
    ), operations


def complete() -> tuple[Any, list[str]]:
    recovery, operations = controller()
    recovery.response(user(), stream("failed"))
    body = failure()
    original = copy.deepcopy(body)
    recovery.request(body)
    assert body == original
    recovery.response(body, stream("retry"))
    recovery.response(user("b" * 32), stream("repair", source="output greeting string = 'hello'"))
    return recovery, operations


def test_fragmented_model_response_orders_fault_and_same_turn_retry() -> None:
    recovery, operations = complete()
    assert operations == ["stop", "restart"]
    assert recovery.ignored_calls == {"failed"}
    result = recovery.result()
    result["containers"] = {"created": 2, "destroyed": 2}
    assert recovery_accepted(result)
    recovery.request(failure())
    assert operations == ["stop", "restart"]


@pytest.mark.parametrize("mutation", ["wrong-turn", "changed-source", "same-id", "before-failure"])
def test_rejects_invalid_retry(mutation: str) -> None:
    recovery, _ = controller()
    recovery.response(user(), stream("failed"))
    if mutation != "before-failure":
        recovery.request(failure())
    body = user("b" * 32) if mutation == "wrong-turn" else user()
    payload = stream(
        "failed" if mutation == "same-id" else "retry",
        source="output greeting string = 'hello'" if mutation == "changed-source" else None,
    )
    with pytest.raises(RuntimeError):
        recovery.response(body, payload)


@pytest.mark.parametrize(
    "content",
    [
        "The tool failed.",
        json.dumps({"result": {"isError": False}}),
        json.dumps({"result": {"content": [{"type": "text", "text": "unavailable"}]}}),
    ],
)
def test_assistant_claim_or_success_cannot_trigger_restart(content: str) -> None:
    recovery, operations = controller()
    recovery.response(user(), stream("failed"))
    body = failure()
    body["messages"][-1]["content"] = content
    with pytest.raises((RuntimeError, ValueError)):
        recovery.request(body)
    assert operations == ["stop"]


def test_no_retry_and_excess_attempts_fail() -> None:
    recovery, _ = controller()
    recovery.response(user(), stream("failed"))
    recovery.request(failure())
    with pytest.raises(RuntimeError, match="Incomplete"):
        recovery.result()
    recovery, _ = complete()
    with pytest.raises(RuntimeError, match="budget"):
        recovery.response(user("c" * 32), stream("extra"))


def test_only_the_verified_failure_is_excluded_from_compiler_results(tmp_path: Any) -> None:
    provider = agent.Provider(tmp_path / "evidence", "", "", "")
    try:
        provider.ignored_calls.add("failed")
        body = {"messages": messages(record())}
        for message in body["messages"]:
            for call in message.get("tool_calls", []):
                call["id"] = "failed"
            if message["role"] == "tool":
                message["tool_call_id"] = "failed"
                message["content"] = '{"error": "unavailable"}'
        provider.observe(body)
        assert provider.records == []
        provider.ignored_calls.clear()
        provider.observe({"messages": messages(record())})
        assert (
            agent.check_turn(
                provider.records, TURN, repaired=False, image=IMAGE, config_hash=CONFIG
            )["calls"]
            == 1
        )
    finally:
        provider.close()


def events() -> list[dict[str, Any]]:
    return [
        {
            "Type": "container",
            "Action": action,
            "timeNano": stamp,
            "Actor": {"ID": identity * 64, "Attributes": {"maf-sandbox.scope": "owned"}},
        }
        for identity, action, stamp in [
            ("a", "create", 2),
            ("a", "destroy", 3),
            ("b", "create", 4),
            ("b", "destroy", 5),
        ]
    ]


def test_exact_container_lifecycle_accepts_two_executions() -> None:
    assert agent.check_container_events(events(), "owned", 1) == {"created": 2, "destroyed": 2}


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "foreign", "early", "reversed", "extra"]
)
def test_rejects_incomplete_or_duplicate_execution(mutation: str) -> None:
    items = events()
    if mutation == "missing":
        items.pop()
    elif mutation == "duplicate":
        items.append(copy.deepcopy(items[0]))
    elif mutation == "foreign":
        items[0]["Actor"]["Attributes"]["maf-sandbox.scope"] = "foreign"
    elif mutation == "early":
        items[0]["timeNano"] = 1
    elif mutation == "reversed":
        items[1]["timeNano"] = 2
    else:
        extra = copy.deepcopy(items[:2])
        for item in extra:
            item["Actor"]["ID"] = "c" * 64
        items.extend(extra)
    with pytest.raises(RuntimeError):
        agent.check_container_events(items, "owned", 1)


@pytest.mark.parametrize(
    "mutation", ["absent", "count", "order", "same-turn", "identity", "cleanup", "timestamp"]
)
def test_collector_refuses_incomplete_recovery(mutation: str) -> None:
    recovery, _ = complete()
    result = recovery.result()
    result["containers"] = {"created": 2, "destroyed": 2}
    if mutation == "absent":
        result["events"].pop(1)
    elif mutation == "count":
        result["attempts"] = 4
    elif mutation == "order":
        result["events"][0], result["events"][1] = result["events"][1], result["events"][0]
    elif mutation == "same-turn":
        result["events"][3]["turn"] = "c" * 64
    elif mutation == "identity":
        result["events"][3]["call"] = result["events"][0]["call"]
    elif mutation == "cleanup":
        result["containers"]["destroyed"] = 1
    else:
        result["events"][1]["time_ns"] = result["events"][0]["time_ns"]
    assert not recovery_accepted(result)


@pytest.mark.parametrize("mutation", ["valid", "missing-removal", "foreign-owner", "before-ready"])
def test_collector_rechecks_retained_container_events(
    tmp_path: Any, monkeypatch: Any, mutation: str
) -> None:
    recovery, _ = complete()
    value = model_sample()
    value["turns"][1]["calls"] = 1
    value["recovery"] = recovery.result()
    value["recovery"]["containers"] = {"created": 2, "destroyed": 2}
    value["recovery"]["private"] = "PRIVATE_SENTINEL"
    value["recovery"]["events"][0]["private"] = "PRIVATE_SENTINEL"
    (tmp_path / "owner").mkdir()
    (tmp_path / "owner/owner").write_text("a" * 32)
    (tmp_path / "report.json").write_text(json.dumps(value))
    (tmp_path / "tool-evidence.jsonl").write_text("private")
    items = events()
    ready = value["recovery"]["events"][2]["time_ns"]
    for item in items:
        item["timeNano"] += ready
        item["Actor"]["Attributes"]["maf-sandbox.scope"] = "openclaw-bicep-" + "a" * 32
    if mutation == "missing-removal":
        items.pop()
    elif mutation == "foreign-owner":
        items[0]["Actor"]["Attributes"]["maf-sandbox.scope"] = "foreign"
    elif mutation == "before-ready":
        items[0]["timeNano"] = ready
    (tmp_path / "docker-events.jsonl").write_text("\n".join(map(json.dumps, items)))
    monkeypatch.setattr(report, "command", lambda *args: "")
    public, accepted = report.collect(tmp_path, "model", worker_restart=True)
    assert accepted is (mutation == "valid")
    assert "PRIVATE_SENTINEL" not in json.dumps(public)
    assert not report.observations(value, "model")[1]


@pytest.mark.parametrize("mode,platform", [("smoke", "linux"), ("model", "win32")])
def test_restart_requires_explicit_native_linux_model_mode(
    tmp_path: Any, monkeypatch: Any, mode: str, platform: str
) -> None:
    monkeypatch.setattr(agent.sys, "platform", platform)
    output = tmp_path / "private"
    with pytest.raises(RuntimeError, match="real-model mode on native Linux"):
        agent.run(argparse.Namespace(mode=mode, worker_restart=True, output=output))
    assert not output.exists()
