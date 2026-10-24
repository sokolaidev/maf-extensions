"""Network refusals require policy evidence and an independent, correctly attributed origin journal."""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def oracle(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_egress_probe").matches_observation


REQUEST = {
    "method": "GET",
    "path": "/allowed?case=fixture",
    "host": "allowed.example:8000",
    "port": 8000,
}


@pytest.mark.parametrize(
    "observed",
    [
        {"error": "URLError", "errno": -2},
        {"error": "URLError", "errno": 111},
        {"error": "TimeoutError"},
        {"status": 200},
    ],
)
def test_network_failure_is_not_policy_refusal(oracle, observed):
    assert not oracle(observed, [], "EACCES", 0, REQUEST)


@pytest.mark.parametrize("body", ["", "upstream denied", '{"error":"application_denied"}', "[]"])
def test_arbitrary_http_403_is_not_policy_evidence(oracle, body):
    assert not oracle({"status": 403, "body": body}, [], 403, 0, REQUEST)


@pytest.mark.parametrize(
    "hits",
    [[], [dict(REQUEST, method="POST")], [dict(REQUEST, host="other.example")], [REQUEST, REQUEST]],
)
def test_success_requires_exact_origin_attribution(oracle, hits):
    assert not oracle({"status": 200, "body": json.dumps(REQUEST)}, hits, 200, 1, REQUEST)


def test_origin_side_effect_invalidates_denial(oracle):
    assert not oracle(
        {"status": 403, "body": '{"error":"policy_denied"}'}, [REQUEST], 403, 0, REQUEST
    )


def test_origin_echo_must_match(oracle):
    assert not oracle(
        {"status": 200, "body": json.dumps(dict(REQUEST, path="/other"))},
        [REQUEST],
        200,
        1,
        REQUEST,
    )


def test_positive_success_and_refusal_controls(oracle):
    assert oracle({"status": 200, "body": json.dumps(REQUEST)}, [REQUEST], 200, 1, REQUEST)
    assert oracle({"status": 403, "body": '{"error":"policy_denied"}'}, [], 403, 0, REQUEST)
    assert oracle({"errno": 13}, [], "EACCES", 0, REQUEST)
    assert oracle({"errno": 13}, [REQUEST], "EACCES", 1, REQUEST)


@pytest.mark.parametrize(
    ("host", "expected_host"),
    [
        ("allowed.example:8000", "denied.example:8000"),
        (
            "allowed.example:8000\r\n X-Injected: yes",
            "denied.example:8000%0D%0A X-Injected: yes",
        ),
        ("allowed.example:8000\n X-Injected: yes", "denied.example:8000%0A X-Injected: yes"),
    ],
)
def test_origin_redirect_cannot_inject_response_header_lines(monkeypatch, host, expected_host):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    probe = importlib.import_module("openshell_egress_probe")
    handlers = []

    def server(address, handler):
        handlers.append(handler)
        return SimpleNamespace(serve_forever=lambda: None)

    monkeypatch.setattr(probe, "ThreadingHTTPServer", server)
    probe.serve_origin()
    response = io.BytesIO()
    request = (
        "GET /allowed/redirect?case=fixture&value=%2F HTTP/1.0\r\nHost: " + host + "\r\n\r\n"
    ).encode("ascii")

    class Connection:
        def makefile(self, *args):
            return io.BytesIO(request)

        def sendall(self, data):
            response.write(data)

        def getsockname(self):
            return ("127.0.0.1", 8000)

    handlers[0](Connection(), ("127.0.0.1", 12345), None)
    headers, body = response.getvalue().split(b"\r\n\r\n", 1)
    lines = headers.decode("ascii").split("\r\n")
    assert lines[0] == "HTTP/1.0 302 Found"
    assert lines[3] == "Location: http://" + expected_host + "/allowed?case=fixture&value=%2F"
    assert lines[4] == "Content-Length: " + str(len(body))
    assert len(lines) == 5
    assert json.loads(body)["host"] == host


@pytest.fixture
def native_probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_egress_probe")


def test_suite_wildcard_and_prefix_require_recursive_upstream_patterns(native_probe):
    from maf_sandbox import EgressRule

    assert native_probe.rule_plan(
        (EgressRule("*.Example.COM", methods=("GET",), paths=("/v1/*",)),)
    ) == [{"host": "**.example.com", "methods": ["GET"], "paths": ["/v1", "/v1/**"]}]
    assert native_probe.rule_plan(()) == []


@pytest.mark.parametrize("value", ["127.0.0.1", "169.254.1.1", "8.8.8.8", "::1"])
def test_origin_must_be_private_nonlocal_ipv4(native_probe, value):
    with pytest.raises(ValueError):
        native_probe.native_origin_ip(value)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"methods": ("PUT",), "paths": ("/a",)},
        {"methods": ("GET",), "paths": ("/a%2fb",)},
        {"methods": ("GET",), "paths": ("/a//b",)},
        {"methods": ("GET",), "paths": ("/a",), "authority": "audience"},
    ],
)
def test_unqualified_rules_are_refused_before_compilation(native_probe, options):
    from maf_sandbox import EgressRule

    with pytest.raises(ValueError):
        native_probe.rule_plan((EgressRule("example.com", **options),))


def test_provider_and_overlapping_host_authority_are_not_silently_widened(native_probe):
    from maf_sandbox import EgressRule

    rule = EgressRule("example.com", methods=("GET",), paths=("/a",))
    with pytest.raises(ValueError, match="provider authority"):
        native_probe.rule_plan((rule,), providers=("additional-authority",))
    with pytest.raises(ValueError, match="duplicate host"):
        native_probe.rule_plan((rule, rule))


def test_inflight_completion_is_not_instant_revocation(native_probe):
    first = dict(REQUEST, path="/allowed/hold?case=fixture")
    observed = [
        {"status": 200, "body": json.dumps(first)},
        {"status": 403, "body": '{"error":"policy_denied"}'},
    ]
    result = native_probe.inflight_observation(observed, [first], first)
    assert result["response_completed_after_loaded"]
    assert result["later_outcome"] == "explicit_policy_refusal"
    assert not result["instant_revocation_qualified"]
    assert not result["same_connection"]
    observed[0]["connection_generation"] = observed[1]["connection_generation"] = 1
    assert native_probe.inflight_observation(observed, [first], first)["same_connection"]
    observed[1]["connection_generation"] = 2
    assert not native_probe.inflight_observation(observed, [first], first)["same_connection"]
    observed[1] = {"error": "ConnectionResetError"}
    assert native_probe.inflight_observation(observed, [first], first)["later_outcome"].startswith(
        "connection_closed"
    )


@pytest.mark.parametrize(
    "failure", ["unrelated_echo", "extra_hit", "wrong_host", "missing_response", "timeout"]
)
def test_inflight_evidence_refuses_unattributed_outcomes(native_probe, failure):
    first = dict(REQUEST, path="/allowed/hold?case=fixture")
    observed = [{"status": 200, "body": json.dumps(first)}, {"errno": 13}]
    hits = [first]
    if failure == "unrelated_echo":
        observed[0]["body"] = "{}"
    elif failure == "extra_hit":
        hits.append(first)
    elif failure == "wrong_host":
        hits = [dict(first, host="other.example")]
    elif failure == "missing_response":
        observed.pop()
    else:
        observed[1] = {"error": "TimeoutError"}
    with pytest.raises(RuntimeError):
        native_probe.inflight_observation(observed, hits, first)


def test_origin_holds_response_until_independent_release(native_probe, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    waiting = threading.Event()
    handlers = []

    class Release:
        def __init__(self):
            self.event = threading.Event()

        def wait(self, timeout):
            waiting.set()
            return self.event.wait(5)

        def set(self):
            self.event.set()

    monkeypatch.setattr(
        native_probe,
        "threading",
        SimpleNamespace(Event=Release, Lock=threading.Lock, Thread=threading.Thread),
    )

    def server(address, handler):
        handlers.append(handler)
        return SimpleNamespace(serve_forever=lambda: None)

    monkeypatch.setattr(native_probe, "ThreadingHTTPServer", server)
    native_probe.serve_origin(revocation_controls=True)

    def request(path, response):
        raw = f"GET {path} HTTP/1.1\r\nHost: allowed.example:8000\r\nConnection: close\r\n\r\n".encode()

        class Connection:
            def makefile(self, *args):
                return io.BytesIO(raw)

            def sendall(self, data):
                response.write(data)

            def getsockname(self):
                return ("127.0.0.1", 8000)

        handlers[0](Connection(), ("127.0.0.1", 12345), None)

    held, journal, released = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(request, "/allowed/hold?case=barrier", held)
        assert waiting.wait(2)
        assert held.getvalue() == b""
        request("/__events", journal)
        hits = json.loads(journal.getvalue().split(b"\r\n\r\n", 1)[1])
        assert hits[0]["path"] == "/allowed/hold?case=barrier"
        request("/__release?case=barrier", released)
        pending.result(timeout=2)
    assert json.loads(held.getvalue().split(b"\r\n\r\n", 1)[1]) == hits[0]


@pytest.mark.parametrize(
    "close_first,fail_reconnect", [(False, False), (True, False), (True, True)]
)
def test_persistent_guest_uses_hostname_without_proxy_environment(
    native_probe, close_first, fail_reconnect
):
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            calls.append((self.path, self.headers["Host"]))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            if close_first and len(calls) == 1:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        host = "localhost:" + str(server.server_port)
        code = native_probe.persistent_request_code("http://" + host + "/allowed/hold?case=x")
        if fail_reconnect:
            code = (
                "import socket\n_original=socket.create_connection; _attempts=0\n"
                "def create(*args,**kwargs):\n global _attempts\n _attempts+=1\n"
                " if _attempts>1: raise PermissionError(13,'refused')\n"
                " return _original(*args,**kwargs)\nsocket.create_connection=create\n"
            ) + code
        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                env={key: value for key, value in os.environ.items() if "proxy" not in key.lower()},
                check=True,
                capture_output=True,
                encoding="utf-8",
                timeout=10,
            )
        finally:
            server.shutdown()
            worker.join(timeout=2)
    observed = json.loads(result.stdout)
    assert [item["connection_generation"] for item in observed] == [1, 2 if close_first else 1]
    if fail_reconnect:
        assert observed[1]["errno"] == 13
        assert calls == [("/allowed/hold?case=x", host)]
    else:
        assert [item["status"] for item in observed] == [200, 200]
        assert calls == [("/allowed/hold?case=x", host), ("/allowed/after?case=x", host)]
