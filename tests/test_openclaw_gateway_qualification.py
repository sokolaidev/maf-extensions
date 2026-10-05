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
        "exchange": "cancel-exchange",
        "time_ns": 11,
    }
    response = {
        "event": "response",
        "exchange": cancel["exchange"],
        "session": call["session"],
        "status": 202,
        "time_ns": 12,
    }
    assert checker.matching_cancel([cancel, response], call)
    assert not checker.matching_cancel([cancel], call)
    assert checker.matching_cancel([cancel, response], call, after_ns=10)
    assert not checker.matching_cancel([cancel, response], call, after_ns=11)
    for changes in [
        {"status": 400},
        {"status": 401},
        {"status": 404},
        {"status": 200},
        {"exchange": "other-exchange"},
        {"session": observer.digest("B")},
        {"event": "settled"},
        {"time_ns": 10},
    ]:
        assert not checker.matching_cancel([cancel, {**response, **changes}], call)
    for changes in [
        {"session": observer.digest("B")},
        {"target": observer.digest("7")},
        {"time_ns": 9},
        {"method": "disconnect"},
    ]:
        assert not checker.matching_cancel([{**cancel, **changes}, response], call)


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
    assert len({record["exchange"] for record in evidence}) == 1
    assert evidence[0]["exchange"]
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


def test_observer_correlates_interleaved_cancellation_responses(tmp_path):
    class App:
        sessions = {"private-session": None}
        service = SimpleNamespace(active=None)

        async def __call__(self, scope, receive, send):
            message = await receive()
            target = json.loads(message["body"])["params"]["requestId"]
            await asyncio.sleep(0)
            await send({"type": "http.response.start", "status": 400 if target == 7 else 202})
            await send({"type": "http.response.body", "body": b""})

    path = tmp_path / "audit.jsonl"
    app = observer.ObserveHTTP(App(), path)

    async def exchange(target):
        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps(
                    {"method": "notifications/cancelled", "params": {"requestId": target}}
                ).encode(),
            }

        async def send(message):
            pass

        await app(
            {
                "type": "http",
                "method": "POST",
                "headers": [(b"mcp-session-id", b"private-session")],
            },
            receive,
            send,
        )

    async def run():
        await asyncio.gather(exchange(7), exchange(8))

    asyncio.run(run())
    evidence = checker.records(path)
    assert [record["event"] for record in evidence[:2]] == ["request", "request"]
    assert len({record["exchange"] for record in evidence}) == 2
    call = {"session": observer.digest(b"private-session".hex()), "time_ns": 1}
    assert not checker.matching_cancel(evidence, {**call, "request": observer.digest(7)})
    assert checker.matching_cancel(evidence, {**call, "request": observer.digest(8)})


lifecycle = load("openclaw_gateway_lifecycle_check")


def loss_evidence():
    return [
        {
            "event": "request",
            "method": "tools/call",
            "exchange": "call",
            "boot": "boot",
            "session": "A",
            "time_ns": 10,
        },
        {
            "event": "result_withheld",
            "exchange": "call",
            "boot": "boot",
            "session": "A",
            "time_ns": 20,
            "completed": True,
            "cleanup": "confirmed",
            "status": 200,
        },
    ]


def test_lost_result_requires_completed_work_and_a_transport_error():
    result = lifecycle.verify_loss(
        loss_evidence(),
        {"status": "error", "error": "Streamable HTTP error: Internal Server Error"},
    )
    assert result["dispatches"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_call",
        "duplicate_result",
        "wrong_exchange",
        "missing_exchange",
        "wrong_boot",
        "wrong_session",
        "stale",
        "incomplete",
        "cleanup_failed",
        "bad_status",
        "missing",
    ],
)
def test_lost_result_rejects_replay_or_uncorrelated_evidence(mutation):
    evidence = loss_evidence()
    if mutation == "duplicate_call":
        evidence.insert(0, evidence[0].copy())
    elif mutation == "duplicate_result":
        evidence.append(evidence[-1].copy())
    elif mutation == "missing":
        evidence.pop()
    else:
        field, value = {
            "wrong_exchange": ("exchange", "other"),
            "missing_exchange": ("exchange", None),
            "wrong_boot": ("boot", "other"),
            "wrong_session": ("session", "B"),
            "stale": ("time_ns", 5),
            "incomplete": ("completed", False),
            "cleanup_failed": ("cleanup", "failed"),
            "bad_status": ("status", 503),
        }[mutation]
        evidence[-1][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_loss(
            evidence, {"status": "error", "error": "Streamable HTTP error: Internal Server Error"}
        )


@pytest.mark.parametrize(
    "result", [{"error": "unrelated"}, projected(), {"error": "500", "structuredContent": {}}]
)
def test_lost_result_refuses_fabricated_or_unrelated_projection(result):
    with pytest.raises(RuntimeError):
        lifecycle.verify_loss(loss_evidence(), result)


def test_observer_fault_withholds_one_complete_result_and_leaves_next_call_transparent(tmp_path):
    emitted = []
    fault = tmp_path / "drop"
    fault.touch()
    payload = json.dumps(
        {"result": {"structuredContent": {"completed": True, "cleanup": "confirmed"}}}
    ).encode()

    class App:
        sessions = {"private-session": None}
        service = SimpleNamespace(active=None)

        async def __call__(self, scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": payload[:12], "more_body": True})
            await send({"type": "http.response.body", "body": payload[12:]})

    async def receive():
        return {"type": "http.request", "body": b'{"method":"tools/call","id":7}'}

    async def send(message):
        emitted.append(message)

    app = observer.ObserveHTTP(App(), tmp_path / "audit.jsonl", fault)
    scope = {"type": "http", "method": "POST", "headers": [(b"mcp-session-id", b"private-session")]}
    with pytest.raises(RuntimeError, match="intentionally withheld"):
        asyncio.run(app(scope, receive, send))
    assert not emitted and not fault.exists()
    first = checker.records(app.evidence)
    assert (
        lifecycle.verify_loss(
            first, {"status": "error", "error": "Streamable HTTP error: Internal Server Error"}
        )["dispatches"]
        == 1
    )
    assert not [r for r in first if r["event"] == "response"]
    assert len({r["boot"] for r in first}) == 1
    asyncio.run(app(scope, receive, send))
    assert b"".join(m.get("body", b"") for m in emitted) == payload
    assert len([r for r in checker.records(app.evidence) if r["event"] == "result_withheld"]) == 1


@pytest.mark.parametrize("reference", ["prepared:latest", "sha256:" + "a" * 64])
def test_image_resolution_uses_docker_identity(reference, monkeypatch):
    calls = []
    image = "sha256:" + "b" * 64

    def inspect(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 15
        return image + "\n"

    monkeypatch.setattr(observer.subprocess, "check_output", inspect)
    assert observer.resolve_image(reference) == image
    assert calls == [["docker", "image", "inspect", "--format", "{{.Id}}", reference]]


@pytest.mark.parametrize(
    "output",
    [
        "",
        "prepared:latest",
        "sha256:abc",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 64 + "\nsha256:" + "b" * 64,
    ],
)
def test_image_resolution_rejects_invalid_docker_identity(output, monkeypatch):
    monkeypatch.setattr(observer.subprocess, "check_output", lambda *args, **kwargs: output)
    with pytest.raises(RuntimeError, match="immutable image ID"):
        observer.resolve_image("prepared:latest")


@pytest.mark.parametrize(
    "expected", [0, checker.BASELINE_DISPATCHES, lifecycle.LIFECYCLE_DISPATCHES]
)
@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_dispatch_count_is_fixed_independently_of_observed_evidence(expected, extra):
    count = max(0, expected + extra)
    evidence = [{"event": "request", "method": "tools/call"}] * count
    evidence += [
        {"event": "request", "method": "tools/list"},
        {"event": "response", "method": "POST"},
    ]
    if count == expected:
        assert checker.verify_dispatch_count(evidence, expected) == count
    else:
        with pytest.raises(RuntimeError, match=f"Expected {expected} MCP dispatches"):
            checker.verify_dispatch_count(evidence, expected)


def test_qualification_launches_service_with_resolved_image(tmp_path, monkeypatch):
    image = "sha256:" + "b" * 64
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "gateway": {"bind": "loopback", "port": 19761, "auth": {}},
                "tools": {"allow": ["bicep__bicep_validate"]},
                "mcp": {
                    "servers": {
                        "bicep": {
                            "transport": "streamable-http",
                            "url": "http://127.0.0.1:19763/mcp",
                            "headers": {},
                        }
                    }
                },
                "models": {
                    "providers": {"qualification": {"baseUrl": "http://127.0.0.1:19762/v1"}}
                },
                "agents": {"defaults": {}},
            }
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    launched = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def connect_ex(self, address):
            return 1

    class Child:
        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, **kwargs):
            return 0

    def start(command, **kwargs):
        launched.append(command)
        if len(launched) == 2:
            raise InterruptedError("captured service launch")
        return Child()

    monkeypatch.setattr(observer.subprocess, "check_output", lambda *args, **kwargs: image)
    monkeypatch.setattr(lifecycle.subprocess, "Popen", start)
    monkeypatch.setattr(lifecycle.socket, "socket", Probe)
    with pytest.raises(InterruptedError, match="captured service launch"):
        lifecycle.qualify(
            SimpleNamespace(
                root=tmp_path / "fresh",
                config=template,
                bicep_config=policy,
                image="prepared:latest",
                openclaw=tmp_path / "openclaw",
            )
        )
    service = launched[1]
    assert service[service.index("--image") + 1] == image
    assert "prepared:latest" not in service


def test_lost_result_allows_schema_names_in_error_text():
    result = {
        "status": "error",
        "error": "Streamable HTTP error: Internal Server Error mentions structuredContent",
    }
    assert lifecycle.verify_loss(loss_evidence(), result)["dispatches"] == 1


@pytest.mark.parametrize(
    "key,value",
    [("result", {}), ("result", {"details": {"structuredContent": {}}}), ("structuredContent", {})],
)
def test_lost_result_rejects_workload_fields_on_error_projection(key, value):
    result = {
        "status": "error",
        "error": "Streamable HTTP error: Internal Server Error",
        key: value,
    }
    with pytest.raises(RuntimeError, match="projected as an outcome"):
        lifecycle.verify_loss(loss_evidence(), result)


def test_image_inspection_failure_preserves_bounded_diagnostic(monkeypatch):
    def inspect(*args, **kwargs):
        raise observer.subprocess.CalledProcessError(
            1, ["docker", "image", "inspect"], stderr="missing local image " + "x" * 5000
        )

    monkeypatch.setattr(observer.subprocess, "check_output", inspect)
    with pytest.raises(
        RuntimeError, match="Docker image inspection failed: missing local image"
    ) as caught:
        observer.resolve_image("sha256:" + "a" * 64)
    assert len(str(caught.value)) <= 4096 + len("Docker image inspection failed: ")


@pytest.mark.parametrize(
    "body",
    [
        b"private invalid source",
        b"\xff",
        b"null",
        b"[]",
        b"{}",
        b'{"result":[]}',
        b'{"result":{"structuredContent":[]}}',
    ],
)
def test_withheld_decode_refuses_malformed_body_without_exposing_it(body):
    with pytest.raises(RuntimeError, match="Withheld response") as caught:
        observer.decode_withheld(body)
    assert hashlib.sha256(body).hexdigest() in str(caught.value)
    assert "private invalid source" not in str(caught.value)
