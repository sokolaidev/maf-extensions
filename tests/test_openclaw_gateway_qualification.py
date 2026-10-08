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


@pytest.mark.parametrize("case", ["registry", "lifecycle"])
@pytest.mark.parametrize("exit_code", [0, 1, 3, -9, None])
def test_final_shutdown_requires_zero_process_exit(case, exit_code):
    evidence = [
        {"event": "shutdown", "poisoned": poisoned, "sessions": 0, "active": False}
        for poisoned in ([False] if case == "registry" else [False, True, False])
    ]
    if exit_code == 0:
        lifecycle.verify_final_shutdown(evidence, case, exit_code)
    else:
        with pytest.raises(RuntimeError, match="Final service shutdown was not clean"):
            lifecycle.verify_final_shutdown(evidence, case, exit_code)


@pytest.mark.parametrize("case", ["registry", "lifecycle"])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "poisoned", "sessions", "active"])
def test_final_shutdown_requires_drained_state_even_with_zero_exit(case, mutation):
    evidence = [
        {"event": "shutdown", "poisoned": poisoned, "sessions": 0, "active": False}
        for poisoned in ([False] if case == "registry" else [False, True, False])
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
