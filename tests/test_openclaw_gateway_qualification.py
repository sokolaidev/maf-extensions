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
        service = SimpleNamespace(active=None, poisoned=False)

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
        service = SimpleNamespace(active=None, poisoned=False)

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
        service = SimpleNamespace(active=None, poisoned=False)

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


def crash_evidence():
    call = loss_evidence()[0]
    return [
        call,
        {
            "event": "abrupt_exit",
            "boot": call["boot"],
            "session": call["session"],
            "active": True,
            "time_ns": 20,
        },
    ]


def test_crash_requires_unfinished_correlated_call_and_unknown_outcome():
    assert lifecycle.verify_crash(
        crash_evidence(), {"status": "error", "error": "fetch failed"}
    ) == {
        "dispatches": 1,
        "unknown_outcome": True,
        "ungraceful_exit": True,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_call",
        "replayed_call",
        "missing_exit",
        "repeated_exit",
        "wrong_boot",
        "wrong_session",
        "inactive",
        "stale_exit",
        "missing_identity",
        "response",
        "settled",
        "result_withheld",
        "shutdown",
    ],
)
def test_crash_refuses_missing_stale_replayed_or_completed_evidence(mutation):
    evidence = crash_evidence()
    if mutation == "missing_call":
        evidence.pop(0)
    elif mutation == "replayed_call":
        evidence.append(evidence[0].copy())
    elif mutation == "missing_exit":
        evidence.pop()
    elif mutation == "repeated_exit":
        evidence.append(evidence[-1].copy())
    elif mutation == "missing_identity":
        evidence[0]["exchange"] = None
    elif mutation in {"response", "settled", "result_withheld", "shutdown"}:
        evidence.append({"event": mutation, "boot": "boot", "exchange": "call"})
    else:
        field, value = {
            "wrong_boot": ("boot", "other"),
            "wrong_session": ("session", "B"),
            "inactive": ("active", False),
            "stale_exit": ("time_ns", 5),
        }[mutation]
        evidence[-1][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_crash(evidence, {"status": "error", "error": "fetch failed"})


@pytest.mark.parametrize(
    "projection",
    [
        {},
        {"status": "error", "error": ""},
        projected(),
        {"status": "error", "error": "fetch failed", "result": {}},
        {"status": "error", "error": "fetch failed", "structuredContent": {}},
    ],
)
def test_crash_refuses_fabricated_workload_projection(projection):
    with pytest.raises(RuntimeError):
        lifecycle.verify_crash(crash_evidence(), projection)


def recovery_evidence():
    return [
        {
            "event": "recovery_observed",
            "boot": "new",
            "container": "orphan",
            "absent": True,
            "owner_empty": True,
            "time_ns": 10,
        },
        {"event": "ready_observed", "boot": "new", "status": 200, "time_ns": 20},
        {"event": "startup_ready", "boot": "new", "owner_empty": True, "time_ns": 5},
    ]


def test_recovery_requires_exact_resource_absence_before_readiness():
    lifecycle.verify_recovery(recovery_evidence(), "new", "orphan")


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "boot", "old"),
        (1, "boot", "old"),
        (0, "container", "another"),
        (0, "absent", False),
        (0, "owner_empty", False),
        (1, "status", 503),
        (0, "time_ns", 21),
        (1, "time_ns", 10),
        (2, "owner_empty", False),
        (2, "boot", "old"),
        (2, "time_ns", 11),
    ],
)
def test_recovery_refuses_wrong_identity_survivors_or_early_readiness(index, field, value):
    evidence = recovery_evidence()
    evidence[index][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_recovery(evidence, "new", "orphan")


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_recovery_refuses_missing_or_duplicate_observations(mutation):
    evidence = recovery_evidence()
    if mutation == "missing":
        evidence.pop()
    else:
        evidence.append(evidence[0].copy())
    with pytest.raises(RuntimeError):
        lifecycle.verify_recovery(evidence, "new", "orphan")


@pytest.mark.parametrize("finished", [None, True, False])
def test_service_crash_hook_only_exits_with_unfinished_work(tmp_path, monkeypatch, finished):
    calls = []
    active = (
        None
        if finished is None
        else (SimpleNamespace(session_id="private-session"), SimpleNamespace(done=lambda: finished))
    )
    app = observer.ObserveHTTP(
        SimpleNamespace(service=SimpleNamespace(active=active)), tmp_path / "audit"
    )

    def terminate(code):
        calls.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(observer.os, "_exit", terminate)
    if finished is False:
        with pytest.raises(SystemExit) as caught:
            observer.crash_active_service(app)
        assert caught.value.code == 86 and calls == [86]
        records = checker.records(app.evidence)
        assert len(records) == 1 and records[0]["event"] == "abrupt_exit"
        assert records[0]["session"] == observer.digest(b"private-session".hex())
        assert "private-session" not in app.evidence.read_text()
    else:
        with pytest.raises(RuntimeError, match="unfinished active work"):
            observer.crash_active_service(app)
        assert not calls and not app.evidence.exists()


@pytest.mark.parametrize("containers", ["", "orphan\n"])
def test_observer_records_owner_state_before_startup_completion(tmp_path, monkeypatch, containers):
    class App:
        async def __call__(self, scope, receive, send):
            await send({"type": "lifespan.startup.complete"})

    def inspect(command, **kwargs):
        assert command[-1] == "label=maf-sandbox.scope=owned"
        return containers

    monkeypatch.setattr(observer.subprocess, "check_output", inspect)
    app = observer.ObserveHTTP(App(), tmp_path / "audit", owned_scope="owned")

    async def receive():
        return {}

    async def send(message):
        evidence = checker.records(app.evidence)
        assert evidence[0]["event"] == "startup_ready"
        assert evidence[0]["owner_empty"] is (not bool(containers))

    asyncio.run(app({"type": "lifespan"}, receive, send))


def refusal_evidence():
    return [
        {"event": "startup", "boot": "refused", "time_ns": 1},
        *[
            {
                "event": "cleanup_refused",
                "boot": "refused",
                "container": "orphan",
                "resource": "bicep-docker",
                "time_ns": t,
            }
            for t in (2, 3)
        ],
        {
            "event": "startup_failed",
            "boot": "refused",
            "time_ns": 4,
            "ready": False,
            "poisoned": True,
            "active": False,
            "sessions": 0,
        },
    ]


def refusal_observation():
    return {
        "boot": "refused",
        "container": "orphan",
        "time_ns": 6,
        "exit_code": 3,
        "paused": True,
        "sole_owned": True,
        "owner_unchanged": True,
        "listener_closed": True,
        "sentinel_preserved": True,
    }


def test_cleanup_refusal_requires_startup_failure_and_two_gateway_errors():
    result = lifecycle.verify_cleanup_refusal(
        refusal_evidence(),
        "refused",
        "orphan",
        refusal_observation(),
        [{"status": "error", "error": 'bundle-mcp server "bicep" is not connected'}] * 2,
    )
    assert result["dispatches"] == 0 and result["gateway_turns_refused"] == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_start",
        "missing_fault",
        "extra_fault",
        "missing_failure",
        "shutdown",
        "wrong_boot",
        "wrong_orphan",
        "wrong_resource",
        "ready",
        "unpoisoned",
        "active",
        "session",
        "early_failure",
        "late_failure",
        "startup_ready",
        "request",
        "response",
    ],
)
def test_cleanup_refusal_rejects_incomplete_contradictory_or_uncorrelated_evidence(mutation):
    evidence = refusal_evidence()
    indices = {"missing_start": 0, "missing_fault": 1, "missing_failure": 3}
    if mutation in indices:
        evidence.pop(indices[mutation])
    elif mutation == "extra_fault":
        evidence.append(evidence[1].copy())
    elif mutation in {"startup_ready", "request", "response", "shutdown"}:
        evidence.append({"event": mutation})
    else:
        index, field, value = {
            "wrong_boot": (1, "boot", "other"),
            "wrong_orphan": (2, "container", "other"),
            "wrong_resource": (1, "resource", "other"),
            "ready": (3, "ready", True),
            "unpoisoned": (3, "poisoned", False),
            "active": (3, "active", True),
            "session": (3, "sessions", 1),
            "early_failure": (3, "time_ns", 2),
            "late_failure": (3, "time_ns", 7),
        }[mutation]
        evidence[index][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_cleanup_refusal(
            evidence,
            "refused",
            "orphan",
            refusal_observation(),
            [{"status": "error", "error": 'bundle-mcp server "bicep" is not connected'}] * 2,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("boot", "other"),
        ("container", "other"),
        ("exit_code", 0),
        ("exit_code", 1),
        ("exit_code", 86),
        ("paused", False),
        ("sole_owned", False),
        ("owner_unchanged", False),
        ("listener_closed", False),
        ("sentinel_preserved", False),
        ("time_ns", 4),
    ],
)
def test_cleanup_refusal_rejects_missing_host_proof(field, value):
    observed = refusal_observation()
    observed[field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_cleanup_refusal(
            refusal_evidence(),
            "refused",
            "orphan",
            observed,
            [{"status": "error", "error": 'bundle-mcp server "bicep" is not connected'}] * 2,
        )


@pytest.mark.parametrize(
    "projections",
    [
        [],
        [{"status": "error", "error": "Unknown tool id"}] * 2,
        [{"status": "error", "error": 'bundle-mcp server "bicep" is not connected'}],
        [projected(), projected()],
        [{"status": "error", "error": ""}] * 2,
        [{"status": "error", "error": 'bundle-mcp server "bicep" is not connected', "result": {}}]
        * 2,
        [
            {
                "status": "error",
                "error": 'bundle-mcp server "bicep" is not connected',
                "structuredContent": {},
            }
        ]
        * 2,
    ],
)
def test_cleanup_refusal_rejects_fabricated_or_missing_gateway_results(projections):
    with pytest.raises(RuntimeError):
        lifecycle.verify_cleanup_refusal(
            refusal_evidence(), "refused", "orphan", refusal_observation(), projections
        )


@pytest.mark.parametrize("owned", ["a" * 12, "b" * 12, "", "a" * 12 + "\n" + "b" * 12])
def test_cleanup_fault_preserves_exact_orphan_and_delegates_after_removal(
    tmp_path, monkeypatch, owned
):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Resource:
        name: str
        cleanup: object

    calls = []

    async def cleanup():
        calls.append("cleanup")
        return True

    resource = Resource("bicep-docker", cleanup)
    service = SimpleNamespace(resources={resource.name: resource})
    app = observer.ObserveHTTP(
        SimpleNamespace(service=service), tmp_path / "audit", owned_scope="owner"
    )
    fault = tmp_path / "fault"
    fault.write_text("a" * 12)

    def inspect(command, **kwargs):
        assert command[-1] == "label=maf-sandbox.scope=owner"
        return owned

    monkeypatch.setattr(observer.subprocess, "check_output", inspect)
    observer.refuse_owned_cleanup(app, fault)
    wrapped = service.resources[resource.name].cleanup
    if owned == "a" * 12:
        assert asyncio.run(wrapped()) is False
        assert checker.records(app.evidence)[0]["event"] == "cleanup_refused"
    else:
        with pytest.raises(RuntimeError, match="retained orphan"):
            asyncio.run(wrapped())
        assert not app.evidence.exists()
    assert not calls
    fault.unlink()
    assert asyncio.run(wrapped()) is True and calls == ["cleanup"]


def test_observer_records_failed_startup_without_retaining_exception_payload(tmp_path):
    class App:
        service = SimpleNamespace(ready=False, poisoned=True, active=None)
        sessions = {}

        async def __call__(self, scope, receive, send):
            await send({"type": "lifespan.startup.failed", "message": "private exception text"})

    app = observer.ObserveHTTP(App(), tmp_path / "audit")

    async def unused():
        return {}

    async def send(message):
        record = checker.records(app.evidence)[0]
        assert record["event"] == "startup_failed" and record["poisoned"] is True
        assert record["ready"] is False and record["active"] is False and record["sessions"] == 0
        assert "private exception text" not in app.evidence.read_text()

    asyncio.run(app({"type": "lifespan"}, unused, send))


def completed_cleanup_evidence():
    common = {"boot": "boot", "session": "session"}
    complete = projected()["result"]["details"]["structuredContent"]
    return [
        dict(common, event="request", method="tools/call", exchange="exchange", time_ns=1),
        dict(common, event="binding_started", time_ns=2),
        dict(common, event="binding_completed", time_ns=3, **complete),
        dict(
            common,
            event="completed_cleanup_refused",
            resource="bicep-docker",
            owner_empty=True,
            time_ns=4,
        ),
        dict(common, event="response", exchange="exchange", status=200, time_ns=5),
        dict(common, event="settled", exchange="exchange", active=False, poisoned=True, time_ns=6),
    ]


def completed_cleanup_projection():
    value = projected()
    value["result"]["details"]["status"] = "error"
    value["result"]["details"]["structuredContent"].update(
        completed=False,
        verdict=None,
        status="cleanup_failed",
        cleanup="failed",
    )
    return value


def test_completed_cleanup_requires_success_before_refusal_and_failed_projection():
    assert (
        lifecycle.verify_completed_cleanup(
            completed_cleanup_evidence(),
            completed_cleanup_projection(),
            "config",
            "image",
        )["success_suppressed"]
        is True
    )


@pytest.mark.parametrize("index", range(6))
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "boot", "session", "time"])
def test_completed_cleanup_rejects_incomplete_uncorrelated_or_reordered_evidence(index, mutation):
    evidence = completed_cleanup_evidence()
    if mutation == "missing":
        evidence.pop(index)
    elif mutation == "duplicate":
        evidence.append(dict(evidence[index]))
    else:
        evidence[index]["time_ns" if mutation == "time" else mutation] = (
            0 if mutation == "time" else "other"
        )
    with pytest.raises(RuntimeError):
        lifecycle.verify_completed_cleanup(
            evidence, completed_cleanup_projection(), "config", "image"
        )


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "exchange", ""),
        (2, "completed", False),
        (2, "verdict", "invalid"),
        (2, "cleanup", "failed"),
        (2, "source_sha256", "other"),
        (2, "config_sha256", "other"),
        (2, "image", "other"),
        (3, "resource", "other"),
        (3, "owner_empty", False),
        (4, "status", 500),
        (4, "exchange", "other"),
        (5, "exchange", "other"),
        (5, "active", True),
        (5, "poisoned", False),
    ],
)
def test_completed_cleanup_rejects_wrong_result_or_resource_state(index, field, value):
    evidence = completed_cleanup_evidence()
    evidence[index][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_completed_cleanup(
            evidence, completed_cleanup_projection(), "config", "image"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("completed", True),
        ("verdict", "valid"),
        ("status", "ok"),
        ("cleanup", "confirmed"),
        ("source_sha256", "other"),
        ("config_sha256", "other"),
        ("image", "other"),
        ("details-status", "ok"),
        ("content", []),
        ("content", [{}, {}]),
    ],
)
def test_completed_cleanup_rejects_success_leaks_and_lost_identity(field, value):
    projection = completed_cleanup_projection()
    if field == "details-status":
        projection["result"]["details"]["status"] = value
    elif field == "content":
        projection["result"][field] = value
    else:
        projection["result"]["details"]["structuredContent"][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_completed_cleanup(
            completed_cleanup_evidence(), projection, "config", "image"
        )


def poisoned_turn_evidence():
    evidence = []
    for index, session in enumerate(["a", "b"]):
        common = {"boot": "boot", "session": session, "exchange": session}
        evidence.extend(
            [
                dict(common, event="request", method="tools/call", time_ns=index * 2 + 1),
                dict(common, event="settled", time_ns=index * 2 + 2, active=False, poisoned=True),
            ]
        )
    projections = [
        {
            "result": {
                "content": [{"type": "text", "text": "Service or session is unavailable."}],
                "details": {"status": "error"},
            }
        }
        for _ in range(2)
    ]
    return evidence, projections


def test_both_sessions_remain_poisoned_without_binding_execution():
    evidence, projections = poisoned_turn_evidence()
    lifecycle.verify_poisoned_turns(evidence, projections, ["a", "b"], "boot")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-call",
        "replay",
        "wrong-session",
        "wrong-boot",
        "missing-settle",
        "active",
        "unpoisoned",
        "stale-settle",
        "binding_started",
        "binding_completed",
        "completed_cleanup_refused",
        "missing-result",
        "success",
        "structured",
        "wrong-error",
    ],
)
def test_poisoned_probes_reject_missing_refusals_or_accidental_work(mutation):
    evidence, projections = poisoned_turn_evidence()
    if mutation == "missing-call":
        evidence.pop(0)
    elif mutation == "replay":
        evidence.append(dict(evidence[0]))
    elif mutation == "missing-settle":
        evidence.pop(1)
    elif mutation in {"wrong-session", "wrong-boot"}:
        evidence[0][mutation.removeprefix("wrong-")] = "other"
    elif mutation in {"active", "unpoisoned", "stale-settle"}:
        key, value = {
            "active": ("active", True),
            "unpoisoned": ("poisoned", False),
            "stale-settle": ("time_ns", 0),
        }[mutation]
        evidence[1][key] = value
    elif mutation in {"binding_started", "binding_completed", "completed_cleanup_refused"}:
        evidence.append({"event": mutation})
    elif mutation == "missing-result":
        projections.pop()
    elif mutation == "success":
        projections[0]["result"]["details"]["status"] = "ok"
    elif mutation == "structured":
        projections[0]["result"]["details"]["structuredContent"] = {}
    else:
        projections[0]["result"]["content"][0]["text"] = "other"
    with pytest.raises(RuntimeError):
        lifecycle.verify_poisoned_turns(evidence, projections, ["a", "b"], "boot")


@pytest.mark.parametrize("completed", [True, False])
def test_completed_cleanup_fault_preserves_result_and_delegates_after_removal(
    tmp_path, monkeypatch, completed
):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Binding:
        tool: object
        execute: object

    @dataclass(frozen=True)
    class Resource:
        name: str
        cleanup: object

    calls = []
    result = SimpleNamespace(
        isError=False, structuredContent=projected()["result"]["details"]["structuredContent"]
    )
    result.structuredContent["completed"] = completed

    async def execute(arguments, context):
        calls.append("execute")
        return result

    async def cleanup():
        calls.append("cleanup")
        return True

    service = SimpleNamespace(
        bindings={"bicep_validate": Binding(SimpleNamespace(name="bicep_validate"), execute)},
        resources={"bicep-docker": Resource("bicep-docker", cleanup)},
        active=None,
        ready=True,
    )
    app = observer.ObserveHTTP(
        SimpleNamespace(service=service), tmp_path / "audit", owned_scope="owner"
    )
    fault = tmp_path / "fault"
    observer.refuse_completed_cleanup(app, fault)

    def inspect(command, **kwargs):
        assert command[-1] == "label=maf-sandbox.scope=owner"
        return ""

    monkeypatch.setattr(observer.subprocess, "check_output", inspect)

    async def scenario():
        resource = service.resources["bicep-docker"]
        binding = service.bindings["bicep_validate"]
        assert await resource.cleanup()
        fault.touch()
        with pytest.raises(RuntimeError, match="retain admission"):
            await resource.cleanup()
        context = SimpleNamespace(session_id="session")
        service.active = (context, None)
        if completed:
            assert await binding.execute({}, context) is result
            assert await resource.cleanup() is False
            assert calls == ["cleanup", "execute"]
            assert [r["event"] for r in checker.records(app.evidence)] == [
                "binding_started",
                "binding_completed",
                "completed_cleanup_refused",
            ]
        else:
            with pytest.raises(RuntimeError, match="completed valid"):
                await binding.execute({}, context)
            with pytest.raises(RuntimeError, match="retain admission"):
                await resource.cleanup()
        fault.unlink()
        assert await resource.cleanup()
        assert calls[-1] == "cleanup"

    asyncio.run(scenario())


def retirement_evidence(active):
    target, other = ("a", "b") if active else ("b", "a")
    call = {"boot": "boot", "session": "a", "request": "request", "time_ns": 1}
    common = {"boot": "boot", "session": target, "exchange": "delete"}
    evidence = [
        dict(common, event="delete_requested", time_ns=3),
        dict(common, event="response", method="DELETE", status=200, time_ns=6),
        dict(
            common,
            event="retired",
            time_ns=9,
            completed=True,
            session_registered=False,
            sessions=1,
            poisoned=False,
            active=not active,
            active_session=None if active else other,
        ),
    ]
    if active:
        evidence.extend(
            [
                {
                    "boot": "boot",
                    "session": target,
                    "exchange": "cancel",
                    "event": "request",
                    "method": "notifications/cancelled",
                    "target": "request",
                    "time_ns": 4,
                },
                {
                    "boot": "boot",
                    "session": target,
                    "exchange": "cancel",
                    "event": "response",
                    "method": "POST",
                    "status": 202,
                    "time_ns": 5,
                },
            ]
        )
    observed = {
        "requested_ns": 2,
        "acknowledged_ns": 8,
        "observed_ns": 10,
        "deleted": True,
        "same_gateway": True,
        "same_service": True,
        "owner_unchanged": True,
        "sentinel_preserved": True,
        "exact_container_absent": active,
        "compiler_survived": not active,
    }
    return evidence, target, other, call, observed


@pytest.mark.parametrize("active", [False, True])
def test_selective_retirement_requires_targeted_delete_and_independent_host_evidence(active):
    evidence, target, other, call, observed = retirement_evidence(active)
    result = lifecycle.verify_retirement(evidence, target, other, call, observed, active=active)
    assert result["accepted_cancellation"] is active
    assert result["other_session_preserved"] is True


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "boot", "session", "exchange", "time_ns"]
)
def test_retirement_rejects_missing_stale_or_wrong_session_deletion(active, index, mutation):
    evidence, target, other, call, observed = retirement_evidence(active)
    if mutation == "missing":
        evidence.pop(index)
    elif mutation == "duplicate":
        evidence.append(dict(evidence[index]))
    else:
        evidence[index][mutation] = 0 if mutation == "time_ns" else "other"
    with pytest.raises(RuntimeError):
        lifecycle.verify_retirement(evidence, target, other, call, observed, active=active)


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "deleted",
        "same_gateway",
        "same_service",
        "owner_unchanged",
        "sentinel_preserved",
        "exact_container_absent",
        "compiler_survived",
    ],
)
def test_retirement_rejects_missing_process_and_container_proof(active, field):
    evidence, target, other, call, observed = retirement_evidence(active)
    observed[field] = not observed[field]
    with pytest.raises(RuntimeError):
        lifecycle.verify_retirement(evidence, target, other, call, observed, active=active)


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("completed", False),
        ("session_registered", True),
        ("sessions", 2),
        ("poisoned", True),
        ("active_session", "wrong"),
    ],
)
def test_retirement_rejects_surviving_registration_or_cross_session_settlement(
    active, field, value
):
    evidence, target, other, call, observed = retirement_evidence(active)
    evidence[2][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_retirement(evidence, target, other, call, observed, active=active)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "target", "session", "boot", "exchange", "rejected", "stale"],
)
def test_active_retirement_requires_accepted_correlated_cancellation(mutation):
    evidence, target, other, call, observed = retirement_evidence(True)
    if mutation == "missing":
        evidence.pop(3)
    elif mutation == "duplicate":
        evidence.append(dict(evidence[3]))
    elif mutation == "rejected":
        evidence[4]["status"] = 400
    else:
        evidence[3]["time_ns" if mutation == "stale" else mutation] = (
            1 if mutation == "stale" else "wrong"
        )
    with pytest.raises(RuntimeError):
        lifecycle.verify_retirement(evidence, target, other, call, observed, active=True)


def test_idle_retirement_rejects_cancellation_of_the_other_call():
    evidence, target, other, call, observed = retirement_evidence(False)
    evidence.append({"event": "request", "method": "notifications/cancelled"})
    with pytest.raises(RuntimeError):
        lifecycle.verify_retirement(evidence, target, other, call, observed, active=False)


def test_retirement_target_pins_only_the_exact_owned_session():
    name = "lifecycle-" + "a" * 32
    row = {"key": "agent:main:openai-user:" + name, "sessionId": "current"}
    assert lifecycle.retirement_target({"sessions": [row]}, name) == {
        "key": row["key"],
        "expectedSessionId": "current",
        "deleteTranscript": False,
    }
    for rows in [[], [row, row], [dict(row, sessionId="")], [dict(row, key="agent:main:main")]]:
        with pytest.raises(RuntimeError):
            lifecycle.retirement_target({"sessions": rows}, name)
    for wrong in ["main", "lifecycle-" + "z" * 32, "lifecycle-" + "a" * 31]:
        with pytest.raises(RuntimeError):
            lifecycle.retirement_target(
                {"sessions": [{"key": "agent:main:openai-user:" + wrong, "sessionId": "current"}]},
                wrong,
            )


@pytest.mark.parametrize("active", [False, True])
def test_delete_observer_records_actual_registration_and_active_session(tmp_path, active):
    context = SimpleNamespace(session_id="active")

    class App:
        sessions = {
            "idle": SimpleNamespace(retiring=None),
            "active": SimpleNamespace(retiring=None),
        }
        service = SimpleNamespace(active=(context, None), poisoned=False)

        async def __call__(self, scope, receive, send):
            selected = "active" if active else "idle"

            async def retire():
                await asyncio.sleep(0)
                self.sessions.pop(selected)
                if active:
                    self.service.active = None
                record.transport.is_terminated = True

            record = self.sessions[selected]
            record.task = asyncio.get_running_loop().create_future()
            record.task.set_result(None)
            record.transport = SimpleNamespace(is_terminated=False, _request_streams={})
            record.ids = set()
            record.requests = 0
            self.sessions[selected].retiring = asyncio.create_task(retire())
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b""})

    path = tmp_path / "audit"
    app = observer.ObserveHTTP(App(), path)

    async def noop(*args):
        return {}

    async def scenario():
        await app(
            {
                "type": "http",
                "method": "DELETE",
                "headers": [(b"mcp-session-id", b"active" if active else b"idle")],
            },
            noop,
            noop,
        )
        rows = checker.records(path)
        assert rows[-1]["event"] == "settled" and rows[-1]["sessions"] == 2
        record = app.app.sessions["active" if active else "idle"]
        await record.retiring
        await asyncio.sleep(0)

    asyncio.run(scenario())
    rows = checker.records(path)
    assert [r["event"] for r in rows] == ["delete_requested", "response", "settled", "retired"]
    assert len({r["exchange"] for r in rows}) == 1
    assert rows[-1]["session_registered"] is False and rows[-1]["sessions"] == 1
    assert rows[-1]["active_session"] == (None if active else observer.digest(b"active".hex()))


@pytest.mark.parametrize(
    "status,payload",
    [
        (200, {"error": {"message": "internal error", "type": "api_error"}}),
        (500, {"choices": []}),
        (503, {"error": {"message": "internal error", "type": "api_error"}}),
        (500, {"error": {"message": "different error", "type": "api_error"}}),
        (500, {"error": {"message": "internal error", "type": "different"}}),
        (500, None),
    ],
)
def test_deleted_turn_rejects_success_or_unrelated_gateway_errors(status, payload):
    with pytest.raises(RuntimeError):
        lifecycle.verify_deleted_turn(status, payload)


def test_deleted_turn_requires_pinned_gateway_abort_projection():
    assert (
        lifecycle.verify_deleted_turn(
            500, {"error": {"message": "internal error", "type": "api_error"}}
        )
        == 500
    )


@pytest.mark.parametrize("method", ["DELETE", "GET", "POST"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_observer_preserves_non_ascii_session_rejection(
    tmp_path, monkeypatch, method, authenticated
):
    monkeypatch.syspath_prepend(str(FIXTURES.parents[1] / "samples/experimental/openclaw_bicep"))
    workload_http = importlib.import_module("workload_http")

    service = SimpleNamespace(ready=True, poisoned=False, active=None)
    token = "a" * 64
    app = workload_http.WorkloadHTTP(service, token, 8765)
    path = tmp_path / "audit"
    observed = observer.ObserveHTTP(app, path)
    headers = [(b"host", app.host), (b"mcp-session-id", b"bad-\xff")]
    if authenticated:
        headers.append((b"authorization", app.authorization))
    scope = {"type": "http", "method": method, "path": "/mcp", "headers": headers}

    async def scenario():
        async def receive():
            raise AssertionError("Rejected headers must not read the body")

        responses = []
        for handler in [app, observed]:
            messages = []

            async def send(message):
                messages.append(message)

            await handler(scope, receive, send)
            responses.append(messages)
        assert responses[0] == responses[1]
        assert responses[1][0]["status"] == (404 if authenticated else 401)
        assert app.sessions == {} and service.active is None and not service.poisoned

    asyncio.run(scenario())
    rows = checker.records(path)
    assert rows[-1]["event"] == "settled"
    assert not any(row["event"] == "retired" for row in rows)
    assert all(row.get("session") == observer.digest(b"bad-\xff".hex()) for row in rows)


def registry_snapshot():
    return {
        "event": "registry_snapshot",
        "boot": "boot",
        "time_ns": 10,
        "readers": 0,
        "active": False,
        "poisoned": False,
        "sessions": [
            {
                "session": str(i),
                "initialized": True,
                "closing": False,
                "sdk_running": True,
                "sdk_terminated": False,
                "sdk_streams": 1,
                "request_ids": 0,
                "requests": 1,
                "get_active": True,
            }
            for i in range(lifecycle.REGISTRY_LIMIT)
        ],
    }


def test_registry_requires_exact_idle_adapter_and_sdk_ownership():
    snapshot = registry_snapshot()
    expected = {str(i) for i in range(lifecycle.REGISTRY_LIMIT)}
    assert lifecycle.verify_registry(snapshot, expected, "boot", 1) == {
        "adapter_records": 8,
        "sdk_tasks": 8,
    }
    snapshot["sessions"][0].update(get_active=False, sdk_streams=0, requests=0)
    lifecycle.verify_registry(snapshot, expected, "boot", 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session", "wrong"),
        ("initialized", False),
        ("closing", True),
        ("sdk_running", False),
        ("sdk_terminated", True),
        ("sdk_streams", 2),
        ("request_ids", 1),
        ("requests", 2),
        ("get_active", 1),
    ],
)
def test_registry_rejects_leaked_or_inconsistent_session_state(field, value):
    snapshot = registry_snapshot()
    snapshot["sessions"][0][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_registry(snapshot, {str(i) for i in range(8)}, "boot", 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "settled"),
        ("boot", "other"),
        ("time_ns", 1),
        ("readers", 1),
        ("active", True),
        ("poisoned", True),
    ],
)
def test_registry_rejects_stale_or_unsettled_snapshot(field, value):
    snapshot = registry_snapshot()
    snapshot[field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_registry(snapshot, {str(i) for i in range(8)}, "boot", 1)


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "expected_over_limit"])
def test_registry_rejects_wrong_capacity_and_duplicate_identity(change):
    snapshot = registry_snapshot()
    expected = {str(i) for i in range(8)}
    if change == "missing":
        snapshot["sessions"].pop()
    elif change == "duplicate":
        snapshot["sessions"][0] = dict(snapshot["sessions"][1])
    else:
        snapshot["sessions"].append(dict(snapshot["sessions"][0], session="extra"))
        if change == "expected_over_limit":
            expected.add("extra")
    with pytest.raises(RuntimeError):
        lifecycle.verify_registry(snapshot, expected, "boot", 1)


def capacity_evidence():
    common = {"boot": "boot", "exchange": "init", "session": None}
    return [
        dict(common, event="request", method="initialize", time_ns=1),
        dict(common, event="response", method="POST", status=503, time_ns=2),
        dict(
            common,
            event="settled",
            method="POST",
            sessions=8,
            active=False,
            poisoned=False,
            time_ns=3,
        ),
    ]


def test_capacity_refusal_requires_initialization_503_without_execution():
    assert lifecycle.verify_registry_refusal(capacity_evidence(), "boot") == {
        "initialization_attempts": 1,
        "status": 503,
    }


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "boot", "exchange", "session", "time_ns"]
)
def test_capacity_refusal_rejects_uncorrelated_or_missing_records(index, mutation):
    evidence = capacity_evidence()
    if mutation == "missing":
        evidence.pop(index)
    elif mutation == "duplicate":
        evidence.append(dict(evidence[index]))
    else:
        evidence[index][mutation] = 0 if mutation == "time_ns" else "wrong"
    with pytest.raises(RuntimeError):
        lifecycle.verify_registry_refusal(evidence, "boot")


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "method", "tools/call"),
        (1, "status", 200),
        (1, "method", "GET"),
        (2, "sessions", 9),
        (2, "sessions", 7),
        (2, "active", True),
        (2, "poisoned", True),
    ],
)
def test_capacity_refusal_rejects_allocation_or_unhealthy_service(index, field, value):
    evidence = capacity_evidence()
    evidence[index][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_registry_refusal(evidence, "boot")


@pytest.mark.parametrize("event", ["binding_started", "retired", "delete_requested"])
def test_capacity_refusal_rejects_work_or_retirement(event):
    with pytest.raises(RuntimeError):
        lifecycle.verify_registry_refusal([*capacity_evidence(), {"event": event}], "boot")


def churn_evidence():
    common = {"boot": "boot", "exchange": "delete", "session": "target"}
    return [
        dict(common, event="delete_requested", time_ns=2),
        dict(common, event="response", method="DELETE", status=200, time_ns=3),
        dict(
            common,
            event="retired",
            time_ns=4,
            completed=True,
            session_registered=False,
            sessions=7,
            active=False,
            active_session=None,
            poisoned=False,
            sdk_finished=True,
            sdk_terminated=True,
            sdk_streams=0,
            request_ids=0,
            requests=0,
        ),
    ]


def test_churn_retirement_requires_sdk_and_adapter_drain():
    lifecycle.verify_churn_retirement(churn_evidence(), "boot", "target", 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("completed", False),
        ("session_registered", True),
        ("sessions", 8),
        ("sessions", 6),
        ("active", True),
        ("active_session", "other"),
        ("poisoned", True),
        ("sdk_finished", False),
        ("sdk_terminated", False),
        ("sdk_streams", 1),
        ("request_ids", 1),
        ("requests", 1),
        ("time_ns", 1),
    ],
)
def test_churn_retirement_rejects_incomplete_or_cross_session_cleanup(field, value):
    evidence = churn_evidence()
    evidence[2][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_churn_retirement(evidence, "boot", "target", 1)


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "boot", "exchange", "session", "time_ns"]
)
def test_churn_retirement_rejects_missing_or_uncorrelated_delete(index, mutation):
    evidence = churn_evidence()
    if mutation == "missing":
        evidence.pop(index)
    elif mutation == "duplicate":
        evidence.append(dict(evidence[index]))
    else:
        evidence[index][mutation] = 0 if mutation == "time_ns" else "wrong"
    with pytest.raises(RuntimeError):
        lifecycle.verify_churn_retirement(evidence, "boot", "target", 1)


@pytest.mark.parametrize("event", ["request", "binding_started"])
def test_idle_churn_rejects_interleaved_dispatch_or_unknown_protocol_activity(event):
    with pytest.raises(RuntimeError):
        lifecycle.verify_churn_retirement(
            [*churn_evidence(), {"event": event}], "boot", "target", 1
        )


def test_observer_registry_snapshot_reads_sdk_counts_and_redacts_identity(tmp_path):
    transport = SimpleNamespace(is_terminated=False, _request_streams={"private-request": object()})
    record = SimpleNamespace(
        initialized=True,
        closing=False,
        task=SimpleNamespace(done=lambda: False),
        transport=transport,
        ids=set(),
        requests=1,
        get_active=True,
    )
    app = SimpleNamespace(
        sessions={"private-session": record},
        readers=0,
        service=SimpleNamespace(active=None, poisoned=False),
    )
    observed = observer.ObserveHTTP(app, tmp_path / "audit")
    snapshot = observed.registry_snapshot()
    row = snapshot["sessions"][0]
    assert row["sdk_running"] and row["sdk_streams"] == row["requests"] == 1
    assert row["session"] == observer.digest(b"private-session".hex())
    assert "private-session" not in json.dumps(snapshot) and "private-request" not in json.dumps(
        snapshot
    )
    transport._request_streams.clear()
    record.task = None
    record.requests = 0
    record.get_active = False
    row = observed.registry_snapshot()["sessions"][0]
    assert not row["sdk_running"] and row["sdk_streams"] == row["requests"] == 0


@pytest.mark.parametrize("mutation", [None, "host", "dependency", "source", "missing_startup"])
def test_shared_environment_check_pins_host_dependencies_and_loaded_sources(tmp_path, mutation):
    host = tmp_path / "host"
    host.mkdir()
    (host / "package.json").write_text(
        json.dumps({"version": "wrong" if mutation == "host" else "2026.9.7"})
    )
    repo = FIXTURES.parents[1]
    versions = {
        "maf-sandbox": "0.46.0",
        "maf-sandbox-bicep": "0.22.0",
        "maf-sandbox-docker": "0.24.4",
        "mcp": "1.28.1",
        "agent-framework-core": "1.19.0",
        "uvicorn": "0.54.0",
    }
    startup = {
        "event": "startup",
        "versions": versions,
        "source_hashes": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                *sorted((repo / "samples/experimental/openclaw_bicep").glob("*.py")),
                FIXTURES / "openclaw_http_observer.py",
            ]
        },
    }
    if mutation == "dependency":
        versions["mcp"] = "wrong"
    if mutation == "source":
        startup["source_hashes"]["openclaw_http_observer.py"] = "wrong"
    evidence = tmp_path / "transport"
    evidence.write_text("" if mutation == "missing_startup" else json.dumps(startup) + "\n")
    args = SimpleNamespace(openclaw=host, transport_evidence=evidence)
    if mutation is None:
        assert checker.verify_environment(args) == {"openclaw": "2026.9.7", "versions": versions}
    else:
        with pytest.raises(RuntimeError):
            checker.verify_environment(args)


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "status",
        "payload",
        "provider_seen",
        "gateway_catalog_error",
        "boot",
        "initialize",
        "tools/list",
        "tools/call",
        "binding_started",
        "retired",
        "delete_requested",
    ],
)
def test_free_slot_catalog_refusal_requires_host_reason_and_no_execution(mutation):
    status = 500
    payload = {"error": {"message": "internal error", "type": "api_error"}}
    observed = {"boot": "boot", "provider_seen": False, "gateway_catalog_error": True}
    evidence = []
    if mutation == "status":
        status = 200
    elif mutation == "payload":
        payload = {}
    elif mutation in {"provider_seen", "gateway_catalog_error"}:
        observed[mutation] = not observed[mutation]
    elif mutation == "boot":
        evidence.append({"boot": "other", "event": "response"})
    elif mutation in {"initialize", "tools/list", "tools/call"}:
        evidence.append({"boot": "boot", "event": "request", "method": mutation})
    elif mutation:
        evidence.append({"boot": "boot", "event": mutation})
    if mutation is None:
        assert (
            lifecycle.verify_catalog_refusal(status, payload, evidence, observed)["reconnected"]
            is False
        )
    else:
        with pytest.raises(RuntimeError):
            lifecycle.verify_catalog_refusal(status, payload, evidence, observed)


def test_idle_registry_checks_tolerate_control_notifications_without_work():
    notification = {"boot": "boot", "event": "request", "method": "notifications/cancelled"}
    lifecycle.verify_registry_refusal([*capacity_evidence(), notification], "boot")
    lifecycle.verify_churn_retirement([*churn_evidence(), notification], "boot", "target", 1)


@pytest.mark.parametrize(
    "case", ["registry", "lifecycle", "unavailable", "idle", "idle-default", "docker-disconnect"]
)
@pytest.mark.parametrize("exit_code", [0, 1, 3, -9, None])
def test_final_shutdown_requires_zero_process_exit(case, exit_code):
    evidence = [
        {"event": "shutdown", "poisoned": poisoned, "sessions": 0, "active": False}
        for poisoned in (
            [False, True, False]
            if case == "lifecycle"
            else [True, False]
            if case == "docker-disconnect"
            else [False]
        )
    ]
    if exit_code == 0:
        lifecycle.verify_final_shutdown(evidence, case, exit_code)
    else:
        with pytest.raises(RuntimeError, match="Final service shutdown was not clean"):
            lifecycle.verify_final_shutdown(evidence, case, exit_code)


@pytest.mark.parametrize(
    "case", ["registry", "lifecycle", "unavailable", "idle", "idle-default", "docker-disconnect"]
)
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "poisoned", "sessions", "active"])
def test_final_shutdown_requires_drained_state_even_with_zero_exit(case, mutation):
    evidence = [
        {"event": "shutdown", "poisoned": poisoned, "sessions": 0, "active": False}
        for poisoned in (
            [False, True, False]
            if case == "lifecycle"
            else [True, False]
            if case == "docker-disconnect"
            else [False]
        )
    ]
    if mutation == "missing":
        evidence.pop()
    elif mutation == "duplicate":
        evidence.append(evidence[-1].copy())
    elif mutation == "poisoned":
        evidence[-1]["poisoned"] = True
    elif mutation == "sessions":
        evidence[-1]["sessions"] = 1
    else:
        evidence[-1]["active"] = True
    with pytest.raises(RuntimeError, match="Final service shutdown was not clean"):
        lifecycle.verify_final_shutdown(evidence, case, 0)


def discovery_refresh_evidence():
    events = []
    for offset, method in enumerate(("initialize", "tools/list")):
        session = None if method == "initialize" else "fresh"
        events.extend(
            [
                {
                    "event": "request",
                    "method": method,
                    "session": session,
                    "time_ns": 2 + offset * 3,
                },
                {
                    "event": "response",
                    "method": "POST",
                    "session": "fresh",
                    "status": 200,
                    "time_ns": 3 + offset * 3,
                },
                {
                    "event": "settled",
                    "method": "POST",
                    "session": session,
                    "sessions": 8,
                    "active": False,
                    "poisoned": False,
                    "time_ns": 4 + offset * 3,
                },
            ]
        )
        for event in events[-3:]:
            event.update(boot="boot", exchange=method)
    return events


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "missing_initialize",
        "duplicate_initialize",
        "missing_listing",
        "duplicate_listing",
        "missing_response",
        "duplicate_response",
        "missing_settlement",
        "duplicate_settlement",
        "wrong_boot",
        "wrong_exchange",
        "reused_exchange",
        "failed_initialize",
        "failed_listing",
        "reused_session",
        "missing_session",
        "initialize_session",
        "listing_session",
        "settled_session",
        "early_initialize",
        "early_listing",
        "response_order",
        "settlement_order",
        "active",
        "poisoned",
        "wrong_count",
        "response_method",
        "settled_method",
        "tool_call",
        "binding",
        "retirement",
        "delete",
    ],
)
def test_discovery_refresh_requires_fresh_correlated_idle_exchanges(mutation):
    evidence = discovery_refresh_evidence()
    if mutation == "missing_initialize":
        del evidence[0]
    elif mutation == "duplicate_initialize":
        evidence.append(evidence[0].copy())
    elif mutation == "missing_listing":
        del evidence[3]
    elif mutation == "duplicate_listing":
        evidence.append(evidence[3].copy())
    elif mutation == "missing_response":
        del evidence[1]
    elif mutation == "duplicate_response":
        evidence.append(evidence[1].copy())
    elif mutation == "missing_settlement":
        del evidence[5]
    elif mutation == "duplicate_settlement":
        evidence.append(evidence[5].copy())
    elif mutation == "wrong_boot":
        evidence[4]["boot"] = "other"
    elif mutation == "wrong_exchange":
        evidence[4]["exchange"] = "other"
    elif mutation == "reused_exchange":
        for event in evidence[3:]:
            event["exchange"] = "initialize"
    elif mutation == "failed_initialize":
        evidence[1]["status"] = 503
    elif mutation == "failed_listing":
        evidence[4]["status"] = 500
    elif mutation == "reused_session":
        evidence[1]["session"] = "previous"
    elif mutation == "missing_session":
        evidence[1].pop("session")
    elif mutation == "initialize_session":
        evidence[0]["session"] = "fresh"
    elif mutation == "listing_session":
        evidence[3]["session"] = "other"
    elif mutation == "settled_session":
        evidence[5]["session"] = "other"
    elif mutation == "early_initialize":
        evidence[0]["time_ns"] = 0
    elif mutation == "early_listing":
        evidence[3]["time_ns"] = 2
    elif mutation == "response_order":
        evidence[4]["time_ns"] = 4
    elif mutation == "settlement_order":
        evidence[5]["time_ns"] = 5
    elif mutation in {"active", "poisoned"}:
        evidence[5][mutation] = True
    elif mutation == "wrong_count":
        evidence[5]["sessions"] = 7
    elif mutation == "response_method":
        evidence[1]["method"] = "GET"
    elif mutation == "settled_method":
        evidence[5]["method"] = "GET"
    elif mutation == "tool_call":
        evidence.append({"boot": "boot", "event": "request", "method": "tools/call"})
    elif mutation in {"binding", "retirement", "delete"}:
        event = {
            "binding": "binding_started",
            "retirement": "retired",
            "delete": "delete_requested",
        }[mutation]
        evidence.append({"boot": "boot", "event": event})
    if mutation is None:
        assert lifecycle.verify_discovery_refresh(evidence, "boot", {"previous"}, 1) == "fresh"
    else:
        with pytest.raises(RuntimeError):
            lifecycle.verify_discovery_refresh(evidence, "boot", {"previous"}, 1)


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "status",
        "payload",
        "service_not_started",
        "port_closed_before",
        "port_closed_after",
        "owner_absent",
        "transport_absent",
        "provider_seen",
        "catalog_error",
    ],
)
def test_initially_unavailable_turn_requires_independent_absence_and_no_provider(mutation):
    status = 500
    payload = {"error": {"message": "internal error", "type": "api_error"}}
    observed = {
        "service_not_started": True,
        "port_closed_before": True,
        "port_closed_after": True,
        "owner_absent": True,
        "transport_absent": True,
        "provider_seen": False,
        "catalog_error": True,
    }
    if mutation == "status":
        status = 200
    elif mutation == "payload":
        payload = {}
    elif mutation:
        observed[mutation] = not observed[mutation]
    if mutation is None:
        lifecycle.verify_unavailable_turn(status, payload, observed)
    else:
        with pytest.raises(RuntimeError):
            lifecycle.verify_unavailable_turn(status, payload, observed)


@pytest.mark.parametrize("count", [1, 2, 8, 0, 9])
def test_discovery_refresh_pins_expected_registry_count(count):
    evidence = discovery_refresh_evidence()
    for event in evidence:
        if event["event"] == "settled":
            event["sessions"] = count
    if 0 < count <= 8:
        assert (
            lifecycle.verify_discovery_refresh(evidence, "boot", set(), 1, expected_count=count)
            == "fresh"
        )
    else:
        with pytest.raises(RuntimeError):
            lifecycle.verify_discovery_refresh(evidence, "boot", set(), 1, expected_count=count)


def test_startup_discovery_rejects_an_unexpected_extra_registration():
    with pytest.raises(RuntimeError):
        lifecycle.verify_discovery_refresh(
            discovery_refresh_evidence(), "boot", set(), 1, expected_count=1
        )


def idle_expiry_evidence():
    live = {
        "session": "active",
        "initialized": True,
        "closing": False,
        "sdk_running": True,
        "sdk_terminated": False,
        "request_ids": 1,
    }
    evidence = [
        {
            "event": "idle_expiry_armed",
            "time_ns": 2,
            "boot": "boot",
            "default_seconds": 900,
            "seconds": 2,
        },
        {
            "event": "idle_expiry_started",
            "time_ns": 3,
            "boot": "boot",
            "session": "idle",
            "from_sweeper": True,
            "idle_seconds": 3,
            "active_idle_seconds": 3,
            "active_session": "active",
            "request_ids": 0,
            "requests": 1,
            "get_active": True,
            "sdk_running": True,
        },
        {
            "event": "idle_expiry_finished",
            "time_ns": 4,
            "boot": "boot",
            "session": "idle",
            "restored_seconds": 900,
            "active_session": "active",
            "completed": True,
            "sdk_finished": True,
            "sdk_terminated": True,
            "session_registered": False,
            "sdk_streams": 0,
            "request_ids": 0,
            "requests": 0,
            "active": True,
            "poisoned": False,
            "sessions": [live],
        },
    ]
    observed = {
        "requested_ns": 1,
        "observed_ns": 5,
        "same_gateway": True,
        "same_service": True,
        "owner_unchanged": True,
        "compiler_survived": True,
        "sentinel_preserved": True,
    }
    return evidence, observed


@pytest.mark.parametrize(
    "stage,field,value",
    [
        (0, "boot", "other"),
        (0, "seconds", 900),
        (0, "default_seconds", 2),
        (0, "time_ns", 0),
        (1, "from_sweeper", False),
        (1, "session", "active"),
        (1, "idle_seconds", 1),
        (1, "active_idle_seconds", 1),
        (1, "active_idle_seconds", None),
        (1, "active_session", None),
        (1, "request_ids", 1),
        (1, "requests", 2),
        (1, "get_active", None),
        (1, "sdk_running", False),
        (1, "time_ns", 1),
        (2, "restored_seconds", 2),
        (2, "active_session", "idle"),
        (2, "completed", False),
        (2, "sdk_finished", False),
        (2, "sdk_terminated", False),
        (2, "session_registered", True),
        (2, "sdk_streams", 1),
        (2, "request_ids", 1),
        (2, "requests", 1),
        (2, "active", False),
        (2, "poisoned", True),
        (2, "sessions", []),
        (2, "time_ns", 8),
    ],
)
def test_idle_expiry_rejects_unproven_timer_retirement(stage, field, value):
    evidence, observed = idle_expiry_evidence()
    evidence[stage][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_idle_expiry(evidence, "boot", "idle", "active", observed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session", "idle"),
        ("initialized", False),
        ("closing", True),
        ("sdk_running", False),
        ("sdk_terminated", True),
        ("request_ids", 0),
    ],
)
def test_idle_expiry_requires_live_active_registration(field, value):
    evidence, observed = idle_expiry_evidence()
    evidence[2]["sessions"][0][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_idle_expiry(evidence, "boot", "idle", "active", observed)


@pytest.mark.parametrize(
    "field",
    ["same_gateway", "same_service", "owner_unchanged", "compiler_survived", "sentinel_preserved"],
)
def test_idle_expiry_requires_independent_survival(field):
    evidence, observed = idle_expiry_evidence()
    observed[field] = False
    with pytest.raises(RuntimeError):
        lifecycle.verify_idle_expiry(evidence, "boot", "idle", "active", observed)


@pytest.mark.parametrize("event", ["request", "delete_requested", "retired", "binding_started"])
def test_idle_expiry_rejects_client_directed_retirement_and_new_work(event):
    evidence, observed = idle_expiry_evidence()
    evidence.append({"event": event, "boot": "boot"})
    with pytest.raises(RuntimeError):
        lifecycle.verify_idle_expiry(evidence, "boot", "idle", "active", observed)


@pytest.mark.parametrize("stage", range(3))
@pytest.mark.parametrize("duplicate", [False, True])
def test_idle_expiry_requires_unique_stages(stage, duplicate):
    evidence, observed = idle_expiry_evidence()
    if duplicate:
        evidence.append(dict(evidence[stage]))
    else:
        evidence.pop(stage)
    with pytest.raises(RuntimeError):
        lifecycle.verify_idle_expiry(evidence, "boot", "idle", "active", observed)


def test_idle_expiry_reports_accelerated_policy_separately():
    evidence, observed = idle_expiry_evidence()
    result = lifecycle.verify_idle_expiry(evidence, "boot", "idle", "active", observed)
    assert result["default_idle_seconds"] == 900
    assert result["fixture_idle_seconds"] == 2
    assert result["expired_sdk_task_finished"] is True


def test_expiry_observer_preserves_sweeper_and_restores_policy(tmp_path):
    async def run():
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        record = SimpleNamespace(
            touched=observer.time.monotonic() - 3,
            ids=set(),
            requests=0,
            get_active=False,
            task=task,
            transport=SimpleNamespace(is_terminated=True, _request_streams={}),
        )
        active_record = SimpleNamespace(touched=observer.time.monotonic() - 3)

        class App:
            idle_seconds = 900
            sessions = {"idle": record, "active": active_record}
            service = SimpleNamespace(active=(SimpleNamespace(session_id="active"), task))

            def retire(self, sid):
                assert self.idle_seconds == 2
                self.sessions.pop(sid)
                return task

            async def _expire(self):
                self.retire("idle")

        app = App()
        evidence = tmp_path / "idle.jsonl"
        observed = observer.ObserveHTTP(app, evidence)
        observed.registry_snapshot = lambda: {"sessions": [], "active": True, "poisoned": False}
        original = app.retire
        observer.arm_idle_expiry(observed)
        assert app.idle_seconds == 2
        assert len(app.sessions) == 2
        await app._expire()
        await asyncio.sleep(0)
        assert app.idle_seconds == 900
        assert app.retire == original
        events = checker.records(evidence)
        assert [r["event"] for r in events] == [
            "idle_expiry_armed",
            "idle_expiry_started",
            "idle_expiry_finished",
        ]
        assert events[1]["from_sweeper"] is True
        assert events[2]["sdk_finished"] is True

    asyncio.run(run())


def default_idle_evidence():
    evidence, _ = idle_expiry_evidence()
    armed, start, end = evidence
    armed.update(accelerated=False, seconds=900, monotonic_ns=1_000_000_000, idle_ages={"idle": 10})
    start.update(
        effective_seconds=900,
        idle_seconds=900,
        active_session=None,
        monotonic_ns=891_000_000_000,
        time_ns=9000,
    )
    end.update(active_session=None, active=False, readers=0, time_ns=9001)
    end["sessions"][0].update(request_ids=0, requests=1, get_active=True, sdk_streams=1)
    for index in range(lifecycle.DEFAULT_IDLE_KEEPALIVES):
        evidence.extend(
            [
                {
                    "event": "request",
                    "method": "tools/call",
                    "boot": "boot",
                    "session": "active",
                    "time_ns": 100 + index,
                    "exchange": f"call-{index}",
                    "request": f"request-{index}",
                },
                {
                    "event": "binding_started",
                    "boot": "boot",
                    "session": "active",
                    "time_ns": 110 + index,
                },
            ]
        )
    return evidence


@pytest.mark.parametrize(
    "stage,field,value",
    [
        (0, "accelerated", True),
        (0, "seconds", 2),
        (0, "default_seconds", 2),
        (0, "idle_ages", {}),
        (0, "idle_ages", {"idle": 60}),
        (0, "monotonic_ns", 2_000_000_000),
        (1, "effective_seconds", 2),
        (1, "idle_seconds", 899.99),
        (1, "from_sweeper", False),
        (1, "session", "active"),
        (1, "active_session", "active"),
        (1, "request_ids", 1),
        (1, "requests", 2),
        (1, "sdk_running", False),
        (1, "time_ns", 1),
        (2, "restored_seconds", 2),
        (2, "completed", False),
        (2, "sdk_finished", False),
        (2, "sdk_terminated", False),
        (2, "session_registered", True),
        (2, "sdk_streams", 1),
        (2, "request_ids", 1),
        (2, "requests", 1),
        (2, "poisoned", True),
        (2, "active", True),
        (2, "sessions", []),
        (2, "readers", 1),
        (2, "boot", "other"),
    ],
)
def test_default_expiry_requires_real_elapsed_time_unchanged_policy_and_drain(stage, field, value):
    evidence = default_idle_evidence()
    evidence[stage][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_default_idle_expiry(evidence, "boot", "idle", "active")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_call",
        "extra_call",
        "wrong_session",
        "duplicate_exchange",
        "missing_binding",
        "wrong_binding",
        "delete",
        "retired",
        "initialize",
    ],
)
def test_default_expiry_accepts_only_bounded_surviving_session_work(mutation):
    evidence = default_idle_evidence()
    if mutation == "missing_call":
        evidence.pop(3)
    elif mutation == "extra_call":
        evidence.append(dict(evidence[3]))
    elif mutation == "wrong_session":
        evidence[3]["session"] = "idle"
    elif mutation == "duplicate_exchange":
        evidence[5]["exchange"] = evidence[3]["exchange"]
    elif mutation == "missing_binding":
        evidence.pop(4)
    elif mutation == "wrong_binding":
        evidence[4]["session"] = "idle"
    elif mutation == "initialize":
        evidence[3]["method"] = "initialize"
    else:
        evidence.append(
            {"boot": "boot", "event": "delete_requested" if mutation == "delete" else "retired"}
        )
    with pytest.raises(RuntimeError):
        lifecycle.verify_default_idle_expiry(evidence, "boot", "idle", "active")


@pytest.mark.parametrize("stage", range(3))
@pytest.mark.parametrize("duplicate", [False, True])
def test_default_expiry_requires_unique_stages(stage, duplicate):
    evidence = default_idle_evidence()
    if duplicate:
        evidence.append(dict(evidence[stage]))
    else:
        evidence.pop(stage)
    with pytest.raises(RuntimeError):
        lifecycle.verify_default_idle_expiry(evidence, "boot", "idle", "active")


def test_default_expiry_reports_default_policy_and_elapsed_observation():
    result = lifecycle.verify_default_idle_expiry(default_idle_evidence(), "boot", "idle", "active")
    assert result["threshold_modified"] is False
    assert result["observed_idle_seconds"] == 900
    assert result["observation_seconds"] == 890
    assert result["keepalive_calls"] == 7


@pytest.mark.parametrize("through_sweeper", [False, True])
def test_default_expiry_observer_never_writes_policy_or_timestamps(tmp_path, through_sweeper):
    async def run():
        task = asyncio.create_task(asyncio.sleep(0))
        await task

        class Record:
            touched = observer.time.monotonic() - 901
            ids = set()
            requests = 0
            get_active = False
            transport = SimpleNamespace(is_terminated=True, _request_streams={})

            def __setattr__(self, name, value):
                assert name != "touched"
                super().__setattr__(name, value)

        record = Record()
        record.task = task

        class App:
            idle_seconds = 900
            sessions = {"idle": record, "survivor": record}
            service = SimpleNamespace(active=None)

            def __setattr__(self, name, value):
                assert name != "idle_seconds"
                super().__setattr__(name, value)

            def retire(self, sid):
                self.sessions.pop(sid)
                return task

            async def _expire(self):
                self.retire("idle")

        app = App()
        evidence = tmp_path / "default.jsonl"
        observed = observer.ObserveHTTP(app, evidence)
        observed.registry_snapshot = lambda: {"sessions": [], "active": False, "poisoned": False}
        original = app.retire
        observer.arm_idle_expiry(observed, accelerated=False)
        assert len(app.sessions) == 2
        if through_sweeper:
            await app._expire()
        else:
            app.retire("idle")
        await asyncio.sleep(0)
        assert app.retire == original
        events = checker.records(evidence)
        assert events[0]["accelerated"] is False
        assert (
            events[0]["seconds"]
            == events[1]["effective_seconds"]
            == events[2]["restored_seconds"]
            == 900
        )
        assert events[1]["from_sweeper"] is through_sweeper

    asyncio.run(run())


def default_idle_control():
    return [
        {
            "event": "request",
            "method": "notifications/cancelled",
            "boot": "boot",
            "session": "idle",
            "exchange": "control",
            "target": "old-request",
            "time_ns": 50,
        },
        {
            "event": "response",
            "method": "POST",
            "boot": "boot",
            "session": "idle",
            "exchange": "control",
            "status": 202,
            "time_ns": 51,
        },
        {
            "event": "settled",
            "method": "POST",
            "boot": "boot",
            "session": "idle",
            "exchange": "control",
            "time_ns": 52,
        },
    ]


def test_default_expiry_accounts_for_accepted_control_traffic_without_new_work():
    result = lifecycle.verify_default_idle_expiry(
        [*default_idle_evidence(), *default_idle_control()], "boot", "idle", "active"
    )
    assert result["control_notifications"] == 1
    assert result["keepalive_calls"] == 7


@pytest.mark.parametrize(
    "stage,field,value",
    [
        (0, "session", "unrelated"),
        (0, "target", None),
        (0, "time_ns", 1),
        (1, "session", "active"),
        (1, "status", 404),
        (1, "exchange", "other"),
        (1, "method", "GET"),
        (2, "session", "active"),
        (2, "exchange", "other"),
        (2, "method", "GET"),
        (2, "time_ns", 9001),
    ],
)
def test_default_expiry_rejects_uncorrelated_or_active_session_control(stage, field, value):
    controls = default_idle_control()
    controls[stage][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_default_idle_expiry(
            [*default_idle_evidence(), *controls], "boot", "idle", "active"
        )


def test_default_expiry_accepts_survivor_control_without_cancelling_observed_work():
    controls = default_idle_control()
    for record in controls:
        record["session"] = "active"
    result = lifecycle.verify_default_idle_expiry(
        [*default_idle_evidence(), *controls], "boot", "idle", "active"
    )
    assert result["control_notifications"] == 1
    controls[0]["target"] = "request-0"
    with pytest.raises(RuntimeError):
        lifecycle.verify_default_idle_expiry(
            [*default_idle_evidence(), *controls], "boot", "idle", "active"
        )


def docker_disconnect_evidence():
    call = {"boot": "boot", "session": "a", "request": "request", "exchange": "call", "time_ns": 1}
    evidence = [
        {
            "event": "request",
            "method": "notifications/cancelled",
            "session": "a",
            "target": "request",
            "exchange": "cancel",
            "time_ns": 4,
        },
        {"event": "response", "session": "a", "exchange": "cancel", "status": 202, "time_ns": 5},
        {
            "event": "docker_removal",
            "target": "compiler",
            "started_ns": 6,
            "time_ns": 7,
            "returncode": 1,
            "connection_refused": True,
            "stderr_sha256": "f" * 64,
        },
        {
            "event": "settled",
            "session": "a",
            "exchange": "call",
            "time_ns": 8,
            "active": False,
            "poisoned": True,
        },
    ]
    for row in evidence:
        row["boot"] = "boot"
    observed = {
        "container": "a" * 64,
        "target": "compiler",
        "owned": ["a" * 64],
        "disconnected_ns": 2,
        "abort_ns": 3,
        "observed_ns": 9,
        "readiness_status": 503,
    }
    observed.update(
        dict.fromkeys(
            (
                "compiler_before",
                "paused",
                "same_gateway",
                "same_service",
                "owner_unchanged",
                "sentinel_preserved",
            ),
            True,
        )
    )
    return evidence, call, observed


def test_docker_disconnect_requires_real_removal_and_retained_work():
    evidence, call, observed = docker_disconnect_evidence()
    result = lifecycle.verify_docker_disconnect(evidence, call, observed)
    assert result["failed_removal_commands"] == 1
    assert result["retained_container"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("container", "a" * 12),
        ("container", "z" * 64),
        ("target", "other"),
        ("owned", []),
        ("owned", ["a" * 64, "b" * 64]),
        ("readiness_status", 200),
        ("compiler_before", False),
        ("paused", False),
        ("same_gateway", False),
        ("same_service", False),
        ("owner_unchanged", False),
        ("sentinel_preserved", False),
        ("disconnected_ns", 4),
        ("abort_ns", 5),
        ("observed_ns", 7),
    ],
)
def test_docker_disconnect_rejects_missing_independent_proof(field, value):
    evidence, call, observed = docker_disconnect_evidence()
    observed[field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_docker_disconnect(evidence, call, observed)


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "boot", "other"),
        (0, "target", "other"),
        (1, "status", 404),
        (1, "exchange", "other"),
        (2, "boot", "other"),
        (2, "target", "other"),
        (2, "returncode", 0),
        (2, "returncode", True),
        (2, "returncode", None),
        (2, "connection_refused", False),
        (2, "stderr_sha256", ""),
        (2, "started_ns", 2),
        (2, "time_ns", 10),
        (3, "active", True),
        (3, "poisoned", False),
        (3, "session", "other"),
        (3, "exchange", "other"),
        (3, "time_ns", 6),
    ],
)
def test_docker_disconnect_rejects_uncorrelated_command_or_cancellation(index, field, value):
    evidence, call, observed = docker_disconnect_evidence()
    evidence[index][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_docker_disconnect(evidence, call, observed)


@pytest.mark.parametrize("index", range(4))
def test_docker_disconnect_rejects_missing_evidence(index):
    evidence, call, observed = docker_disconnect_evidence()
    evidence.pop(index)
    with pytest.raises(RuntimeError):
        lifecycle.verify_docker_disconnect(evidence, call, observed)


def docker_recovery_evidence():
    old = {"boot": "old", "source_hashes": {"source": "hash"}, "versions": {"docker": "version"}}
    return old, [
        {**old, "boot": "new", "event": "startup", "time_ns": 1},
        {
            "boot": "new",
            "event": "docker_removal",
            "target": "compiler",
            "returncode": 0,
            "connection_refused": False,
            "started_ns": 2,
            "time_ns": 3,
        },
        {"boot": "new", "event": "startup_ready", "owner_empty": True, "time_ns": 4},
    ]


def test_docker_recovery_requires_removal_before_readiness():
    old, evidence = docker_recovery_evidence()
    assert lifecycle.verify_docker_recovery(evidence, old, "compiler") == "new"


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "boot", "old"),
        (0, "source_hashes", {}),
        (0, "versions", {}),
        (1, "boot", "other"),
        (1, "target", "other"),
        (1, "returncode", 1),
        (1, "connection_refused", True),
        (1, "started_ns", 0),
        (1, "time_ns", 5),
        (2, "boot", "other"),
        (2, "owner_empty", False),
        (2, "time_ns", 2),
    ],
)
def test_docker_recovery_rejects_unproven_removal(index, field, value):
    old, evidence = docker_recovery_evidence()
    evidence[index][field] = value
    with pytest.raises(RuntimeError):
        lifecycle.verify_docker_recovery(evidence, old, "compiler")


@pytest.mark.parametrize("index", range(3))
def test_docker_recovery_requires_every_stage(index):
    old, evidence = docker_recovery_evidence()
    evidence.pop(index)
    with pytest.raises(RuntimeError):
        lifecycle.verify_docker_recovery(evidence, old, "compiler")


def test_removal_observer_forwards_real_command_and_result(tmp_path):
    calls = []
    result = SimpleNamespace(returncode=1, stderr="connection refused; private endpoint")

    class Backend:
        async def _invoke(self, *args, **kwargs):
            calls.append((args, kwargs))
            return result

    evidence = tmp_path / "evidence.jsonl"
    observer.observe_docker_removal(observer.ObserveHTTP(None, evidence), Backend)
    backend = Backend()
    assert asyncio.run(backend._invoke("rm", "-f", "compiler", timeout=10)) is result
    assert calls == [(("rm", "-f", "compiler"), {"timeout": 10})]
    rows = checker.records(evidence)
    assert len(rows) == 1 and rows[0]["connection_refused"] is True
    assert rows[0]["returncode"] == 1 and rows[0]["target"] == "compiler"
    assert "private endpoint" not in evidence.read_text()
    assert asyncio.run(backend._invoke("ps", "-aq")) is result
    assert len(checker.records(evidence)) == 1


def test_docker_disconnect_uses_only_a_private_context(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(
        lifecycle,
        "docker",
        lambda *args: json.dumps(
            {"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}
        ),
    )

    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    monkeypatch.setenv("DOCKER_HOST", "private-original-host")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    with lifecycle.ExitStack() as stack:
        fault = lifecycle.DockerConnectionFault(tmp_path, stack)
        assert fault.environment["DOCKER_CONFIG"] == str(tmp_path / "docker-config")
        assert fault.environment["DOCKER_CONTEXT"] == fault.name
        assert "DOCKER_HOST" not in fault.environment
        assert "DOCKER_TLS_VERIFY" not in fault.environment
        with lifecycle.socket.socket() as probe:
            assert probe.connect_ex(fault.reserved.getsockname()) != 0
        fault.disconnect()
    assert [row[4] for row in commands] == ["create", "update", "update"]
    assert all(
        row[:4] == ["docker", "--config", str(tmp_path / "docker-config"), "context"]
        for row in commands
    )
    assert commands[1][-1].startswith("host=tcp://127.0.0.1:")
    assert commands[2][-1] == "host=unix:///var/run/docker.sock"


def test_docker_disconnect_refuses_remote_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "docker",
        lambda *args: json.dumps({"Endpoints": {"docker": {"Host": "tcp://remote:2376"}}}),
    )
    with lifecycle.ExitStack() as stack, pytest.raises(RuntimeError, match="local pipe/socket"):
        lifecycle.DockerConnectionFault(tmp_path, stack)


@pytest.mark.parametrize("message", ["permission refused", "could not connect", "", "not found"])
def test_removal_observer_does_not_label_other_errors_as_connection_refusal(tmp_path, message):
    class Backend:
        async def _invoke(self, *args, **kwargs):
            return SimpleNamespace(returncode=1, stderr=message)

    path = tmp_path / "evidence.jsonl"
    observer.observe_docker_removal(observer.ObserveHTTP(None, path), Backend)
    asyncio.run(Backend()._invoke("rm", "-f", "compiler"))
    assert checker.records(path)[0]["connection_refused"] is False
