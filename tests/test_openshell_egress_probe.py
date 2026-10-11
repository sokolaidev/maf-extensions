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
