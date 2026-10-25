"""Raw route evidence must preserve wire targets and distinguish refusal from transport failure."""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_routes_probe")


@pytest.fixture
def origin():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            row = {
                "method": self.command,
                "path": self.path,
                "host": self.headers["Host"],
                "port": cast(ThreadingHTTPServer, self.server).server_port,
            }
            hits.append(row)
            body = json.dumps(row).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, hits
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize("index", range(23))
def test_raw_client_preserves_every_target_before_supervision(probe, origin, index):
    port, hits = origin
    case = dict(probe.route_cases("172.18.0.2")[index], port=port)
    wire, expected = probe.request_material(case, "opaque-marker")
    expected["path"] = (
        case["target"] + ("&" if "?" in case["target"] else "?") + "case=opaque-marker"
    )
    observed = probe.raw_request("127.0.0.1", port, wire)
    assert observed == {"status": 200, "body": json.dumps(expected)}
    assert hits == [expected]


def test_guest_raw_client_ignores_proxy_environment(probe, origin, monkeypatch):
    port, hits = origin
    case = dict(probe.route_cases("172.18.0.2")[1], port=port)
    wire, _ = probe.request_material(case, "guest")
    for name in ("http_proxy", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    result = subprocess.run(
        [sys.executable, "-c", probe.guest_code("127.0.0.1", port, wire)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stderr == ""
    assert json.loads(result.stdout)["status"] == 200
    assert hits[0]["path"] == "/%61llowed/child?case=guest"


REQUEST = {
    "method": "GET",
    "path": "/allowed/child?case=x",
    "host": "allowed.example:8000",
    "port": 8000,
}


@pytest.mark.parametrize(
    "mutation",
    ["raw_path", "other_host", "wrong_port", "wrong_method", "duplicate", "missing", "echo"],
)
def test_forwarding_requires_exact_canonical_origin_and_echo(probe, mutation):
    hits = [REQUEST]
    observed = {"status": 200, "body": json.dumps(REQUEST)}
    if mutation == "duplicate":
        hits *= 2
    elif mutation == "missing":
        hits = []
    elif mutation == "echo":
        observed["body"] = "{}"
    else:
        changes = {
            "raw_path": {"path": "/allowed/./child?case=x"},
            "other_host": {"host": "denied.example"},
            "wrong_port": {"port": 8001},
            "wrong_method": {"method": "POST"},
        }
        hits = [dict(REQUEST, **changes[mutation])]
        observed["body"] = json.dumps(hits[0])
    with pytest.raises(RuntimeError):
        probe.route_observation(observed, hits, 200, REQUEST)


@pytest.mark.parametrize(
    "observed",
    [
        {"error": "TimeoutError", "phase": "response"},
        {"error": "gaierror", "errno": -2, "phase": "connect"},
        {"error": "ConnectionRefusedError", "errno": 111, "phase": "connect"},
        {"status": 403, "body": "{}"},
        {"errno": 13, "phase": "response"},
    ],
)
@pytest.mark.parametrize("expected", ["EACCES", "refused", "unqualified_rejection"])
def test_incidental_failure_cannot_complete_characterization(probe, observed, expected):
    with pytest.raises(RuntimeError):
        probe.route_observation(observed, [], expected, REQUEST)


@pytest.mark.parametrize(
    "observed",
    [
        {"error": "RemoteDisconnected", "phase": "response"},
        {"error": "ConnectionResetError", "phase": "response"},
        {"status": 400, "body": "bad request"},
    ],
)
def test_parser_observation_is_explicitly_unqualified(probe, observed):
    assert probe.route_observation(observed, [], "unqualified_rejection", REQUEST).endswith(
        "_unqualified"
    )
    for expected, hits in [("refused", []), ("EACCES", []), ("unqualified_rejection", [REQUEST])]:
        with pytest.raises(RuntimeError):
            probe.route_observation(observed, hits, expected, REQUEST)


def test_success_and_policy_controls(probe):
    assert (
        probe.route_observation(
            {"status": 200, "body": json.dumps(REQUEST)}, [REQUEST], 200, REQUEST
        )
        == "forwarded_exactly"
    )
    assert (
        probe.route_observation(
            {"status": 403, "body": '{"error":"policy_denied"}'}, [], 403, REQUEST
        )
        == "explicit_policy_refusal"
    )
    assert (
        probe.route_observation(
            {"error": "PermissionError", "errno": 13, "phase": "connect"}, [], "EACCES", REQUEST
        )
        == "connect_permission_refusal"
    )
    with pytest.raises(RuntimeError):
        probe.route_observation({"errno": 13, "phase": "connect"}, [REQUEST], "EACCES", REQUEST)


@pytest.mark.parametrize("failure", [False, True])
def test_report_never_promotes_parser_closures_and_always_disposes(probe, monkeypatch, failure):
    import ast
    from types import SimpleNamespace

    journal, disposed, reports = [], [], []
    ip = "172.18.0.2"
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(None, None, None, None, (ip, 8000))]
    )
    monkeypatch.setattr(probe, "fixture_policy", lambda image, rules, ip: bool(rules))
    monkeypatch.setattr(probe, "origin_get", lambda *args: list(journal))

    def record(destination, port, wire):
        lines = wire.split("\r\n")
        method, target, _ = lines[0].split(" ")
        row = {"method": method, "path": target, "host": lines[1][6:], "port": port}
        journal.append(row)
        return {"status": 200, "body": json.dumps(row)}

    monkeypatch.setattr(probe, "raw_request", record)
    # Keep guest generation independent of the replaced host control function.
    monkeypatch.setattr(
        probe, "guest_code", lambda destination, port, wire: repr((destination, port, wire))
    )

    class Fixture:
        client = SimpleNamespace(image="fixture-image")

        def __init__(self):
            self.report = {}

        def save(self):
            reports.append(json.loads(json.dumps(self.report)))

        async def acquire(self, *, policy):
            cases = iter(probe.route_cases(ip, closed=not policy))

            async def execute(argv, **kwargs):
                case = next(cases)
                destination, port, wire = ast.literal_eval(argv[-1])
                if failure:
                    observed = {"error": "TimeoutError", "phase": "response"}
                elif case["expected"] == 200:
                    target = wire.split(" ")[1]
                    suffix = target[target.index("?") :]
                    wire = wire.replace(target, case["forwarded"].split("?")[0] + suffix, 1)
                    observed = record(destination, port, wire)
                elif case["expected"] in (403, "refused"):
                    observed = {"status": 403, "body": '{"error":"policy_denied"}'}
                elif case["expected"] == "EACCES":
                    observed = {"error": "PermissionError", "errno": 13, "phase": "connect"}
                else:
                    observed = {"error": "RemoteDisconnected", "phase": "response"}
                return SimpleNamespace(exit_code=0, stderr_bytes=b"", stdout=json.dumps(observed))

            async def dispose():
                disposed.append(policy)

            return SimpleNamespace(exec=execute, dispose=dispose)

    fixture = Fixture()
    if failure:
        with pytest.raises(RuntimeError, match="unexpected raw HTTP"):
            asyncio.run(probe.measure_routes(fixture, ip))
        assert disposed == [True]
        assert not fixture.report["routes"].get("characterization_completed")
        assert reports[-1]["routes"]["cases"][-1]["guest"]["error"] == "TimeoutError"
    else:
        asyncio.run(probe.measure_routes(fixture, ip))
        assert disposed == [True, False]
        assert fixture.report["routes"]["characterization_completed"]
        assert len(fixture.report["routes"]["cases"]) == 28
        assert len(fixture.report["routes"]["controls"]) == 28
    assert not fixture.report["routes"]["qualification_passed"]
