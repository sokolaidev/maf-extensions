"""Evidence isolation and fail-closed checks for manual OpenClaw qualification."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import http.client
import importlib.util
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    spec = importlib.util.spec_from_file_location(name, FIXTURES / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


provider = load("openclaw_gateway_provider")
load("openclaw_gateway_check")
checker = load("openclaw_gateway_http_check")
observer = load("openclaw_http_observer")


def projected(case="valid"):
    canonical = json.dumps([["main.bicep", provider.SOURCES[case]]], separators=(",", ":")).encode()
    return {
        "result": {
            "content": [{"type": "text", "text": "result"}],
            "details": {
                "structuredContent": {
                    "completed": case != "incomplete",
                    "verdict": None if case == "incomplete" else case,
                    "status": "incomplete" if case == "incomplete" else "ok",
                    "cleanup": "confirmed",
                    "source_sha256": hashlib.sha256(canonical).hexdigest(),
                    "config_sha256": "config",
                    "image": "image",
                }
            },
        }
    }


@pytest.mark.parametrize("case", ["valid", "invalid", "incomplete"])
def test_check_host_identity_and_projection(case):
    result = checker.verify_outcome(projected(case), case, "config", "image")
    assert result["projected_content_items"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("completed", False),
        ("verdict", "invalid"),
        ("status", "incomplete"),
        ("cleanup", "failed"),
        ("source_sha256", "other"),
        ("config_sha256", "other"),
        ("image", "other"),
    ],
)
def test_check_rejects_wrong_outcome_or_identity(field, value):
    result = projected()
    result["result"]["details"]["structuredContent"][field] = value
    with pytest.raises(RuntimeError):
        checker.verify_outcome(result, "valid", "config", "image")


def test_check_rejects_duplicate_projection():
    result = projected()
    result["result"]["content"] *= 2
    with pytest.raises(RuntimeError, match="duplicated"):
        checker.verify_outcome(result, "valid", "config", "image")


def test_results_are_correlated_to_one_turn_and_scenario():
    record = {
        "turn": "current",
        "scenario": "valid",
        "tool_result": "untrusted wrapper\n" + json.dumps(projected()) + "\nend",
    }
    stale = dict(record, turn="stale")
    assert checker.projected_result([stale, record], "current", "valid") == projected()
    for evidence, case in [([stale], "valid"), ([record, record], "valid"), ([record], "invalid")]:
        with pytest.raises(RuntimeError):
            checker.projected_result(evidence, "current", case)


def test_cancel_requires_later_message_for_exact_session_and_typed_request():
    call = {"session": observer.digest("A"), "request": observer.digest(7), "time_ns": 10}
    cancel = {
        "event": "request",
        "method": "notifications/cancelled",
        "session": call["session"],
        "target": call["request"],
        "time_ns": 11,
    }
    assert checker.matching_cancel([cancel], call)
    for changes in [
        {"session": observer.digest("B")},
        {"target": observer.digest("7")},
        {"time_ns": 9},
        {"method": "disconnect"},
    ]:
        assert not checker.matching_cancel([{**cancel, **changes}], call)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"arguments": {"source": "secret source"}},
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 7, "reason": "secret source"},
        },
        {"method": "notifications/cancelled", "params": []},
        {"method": {"malformed": True}},
    ],
)
def test_observer_preserves_chunked_messages_and_redacts(tmp_path, payload):
    body = json.dumps(payload).encode()
    messages = [
        {"type": "http.request", "body": body[:8], "more_body": True},
        {"type": "http.request", "body": body[8:]},
    ]
    original = copy.deepcopy(messages)
    emitted = []

    class App:
        sessions = {"private-session": None}
        service = SimpleNamespace(active=None)

        async def __call__(self, scope, receive, send):
            for expected in original:
                assert await receive() == expected
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"mcp-session-id", b"private-session")],
                }
            )
            await send({"type": "http.response.body", "body": b"untouched response"})

    async def receive():
        return messages.pop(0)

    async def send(message):
        emitted.append(message)

    path = tmp_path / "audit.jsonl"
    app = observer.ObserveHTTP(App(), path)
    asyncio.run(
        app(
            {
                "type": "http",
                "method": "POST",
                "headers": [
                    (b"mcp-session-id", b"private-session"),
                    (b"authorization", b"Bearer private-token"),
                ],
            },
            receive,
            send,
        )
    )
    text = path.read_text()
    for secret in ("private-session", "private-token", "secret source", "untouched response"):
        assert secret not in text
    assert emitted[-1]["body"] == b"untouched response"
    evidence = checker.records(path)
    assert evidence[-1]["sessions"] == 1 and evidence[-1]["active"] is False
    if payload.get("method") == "tools/call":
        assert evidence[0]["request"] == observer.digest(7)
    if isinstance(payload.get("params"), dict) and "requestId" in payload["params"]:
        assert evidence[0]["target"] == observer.digest(7)


def test_provider_concurrent_turns_do_not_mix_evidence(tmp_path):
    evidence = tmp_path / "provider.jsonl"
    with ThreadingHTTPServer(("127.0.0.1", 0), provider.handler(evidence)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(number):
            turn = f"{number:032x}"
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    json.dumps(
                        {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": f"qualification valid qualification-turn={turn}",
                                },
                                {"role": "tool", "content": turn},
                            ],
                            "tools": [],
                        }
                    ),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                assert response.status == 200
                response.read()
            finally:
                connection.close()

        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(request, range(16)))
        finally:
            server.shutdown()
            thread.join(timeout=5)
    records = checker.records(evidence)
    assert len(records) == 16
    assert len({record["turn"] for record in records}) == 16
    assert all(record["tool_result"] == record["turn"] for record in records)


def test_partial_evidence_line_is_not_a_result(tmp_path):
    path = tmp_path / "evidence.jsonl"
    path.write_bytes(b'{"complete": true}\n{"partial":')
    assert checker.records(path) == [{"complete": True}]


def test_failed_check_removes_old_success_report(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text('{"passed": true}')
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check",
            *[
                value
                for name in (
                    "config",
                    "owner",
                    "bicep-config",
                    "evidence",
                    "transport-evidence",
                    "openclaw",
                )
                for value in ("--" + name, str(tmp_path / name))
            ],
            "--report",
            str(report),
            "--image",
            "image",
        ],
    )
    with pytest.raises(FileNotFoundError):
        checker.main()
    assert not report.exists()


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"status": "ok"},
        {"status": "error", "structuredContent": {}},
        {"status": "error", "structuredContent": {"verdict": "valid"}},
    ],
)
def test_busy_requires_error_without_fabricated_result(details):
    with pytest.raises(RuntimeError):
        checker.verify_busy(
            {"result": {"content": [{"type": "text", "text": "Service busy"}], "details": details}}
        )


def test_pinned_gateway_busy_projection():
    checker.verify_busy(
        {
            "result": {
                "content": [{"type": "text", "text": "Service busy"}],
                "details": {"status": "error"},
            }
        }
    )
    assert not checker.matching_cancel(
        [{"event": "request", "method": "notifications/cancelled", "time_ns": 2}], {"time_ns": 1}
    )
