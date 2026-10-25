"""HTTPS qualification rejects incidental disconnects and unbound origin echoes."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

URL = "https://httpbingo.org/anything?case=fixture"
ALERT = {"handshake_error": "SSLV3_ALERT_CERTIFICATE_UNKNOWN"}


@pytest.fixture
def oracle(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_https_probe").matches_https


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"handshake_error": "UNEXPECTED_EOF_WHILE_READING"}],
        [ALERT, {"path": "/anything"}],
        [ALERT, ALERT],
    ],
)
def test_disconnect_needs_exact_certificate_alert_and_no_http_request(oracle, events):
    assert not oracle({"error": "RemoteDisconnected"}, "certificate_refused", URL, "GET", events)


@pytest.mark.parametrize(
    "observed", [{"status": 200}, {"error": "TimeoutError"}, {"error": "URLError", "errno": -2}]
)
def test_origin_alert_does_not_validate_an_unrelated_guest_failure(oracle, observed):
    assert not oracle(observed, "certificate_refused", URL, "GET", [ALERT])


def test_certificate_refusal_requires_both_observers(oracle):
    assert oracle({"error": "RemoteDisconnected"}, "certificate_refused", URL, "GET", [ALERT])


@pytest.mark.parametrize(
    "body",
    [{"url": URL, "method": "POST"}, {"url": URL + "-other", "method": "GET"}, [], "not JSON"],
)
def test_success_requires_exact_method_and_correlated_url(oracle, body):
    assert not oracle({"status": 200, "body": json.dumps(body)}, 200, URL, "GET")


def test_https_success_and_policy_controls(oracle):
    assert oracle(
        {"status": 200, "body": json.dumps({"url": URL, "method": "GET"})}, 200, URL, "GET"
    )
    assert oracle({"status": 403, "body": '{"error":"policy_denied"}'}, 403, URL, "GET")
    assert not oracle({"status": 403, "body": "application denied"}, 403, URL, "GET")
    assert oracle({"errno": 13}, "EACCES", URL, "GET")


@pytest.fixture
def native_tls(monkeypatch, tmp_path):
    import socket
    import ssl

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    probe = importlib.import_module("openshell_https_probe")
    directory = tmp_path / "tls"
    probe.prepare_native_tls(directory, "10.23.45.67")
    servers, events = probe.native_tls_servers(directory, bind="127.0.0.1", ports=(0, 0, 0, 0))
    context = ssl.create_default_context(cafile=str(directory / "ca.pem"))

    def connect(index, hostname="allowed.10.23.45.67.nip.io", method="GET"):
        import http.client

        port = servers[index].server_port
        raw = socket.create_connection(("127.0.0.1", port), timeout=3)
        try:
            secured = context.wrap_socket(raw, server_hostname=hostname)
        except BaseException:
            raw.close()
            raise
        connection = http.client.HTTPConnection(hostname, port, timeout=3)
        connection.sock = secured
        try:
            connection.request(method, "/anything?case=local")
            response = connection.getresponse()
            return {"status": response.status, "body": response.read().decode()}
        finally:
            connection.close()

    try:
        yield probe, connect, servers, events, directory
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_owned_tls_roundtrip_and_independent_origin_attribution(native_tls):
    probe, connect, servers, events, _ = native_tls
    observed = connect(0)
    expected = {
        "method": "GET",
        "path": "/anything?case=local",
        "host": f"allowed.10.23.45.67.nip.io:{servers[0].server_port}",
        "port": servers[0].server_port,
    }
    assert probe.matches_native_https(observed, events, 200, expected)
    assert not probe.matches_native_https(observed, [], 200, expected)
    assert not probe.matches_native_https(observed, events * 2, 200, expected)
    assert not probe.matches_native_https(observed, events, 200, dict(expected, method="POST"))
    assert connect(0, method="POST")["status"] == 200


@pytest.mark.parametrize("index,code", [(1, 62), (2, 10), (3, 20)])
def test_real_certificates_fail_for_the_intended_verification_reason(native_tls, index, code):
    import ssl

    _, connect, _, events, _ = native_tls
    with pytest.raises(ssl.SSLCertVerificationError) as error:
        connect(index)
    assert error.value.verify_code == code
    assert not any("method" in event for event in events)
    assert connect(0)["status"] == 200
    if index == 1:
        assert connect(1, hostname="other.10.23.45.67.nip.io")["status"] == 200


@pytest.mark.parametrize(
    "observed,events",
    [
        (
            {"error": "TimeoutError"},
            [{"port": 8444, "handshake_error": "SSLV3_ALERT_CERTIFICATE_UNKNOWN"}],
        ),
        ({"error": "RemoteDisconnected"}, []),
        (
            {"error": "RemoteDisconnected"},
            [{"port": 8443, "handshake_error": "SSLV3_ALERT_CERTIFICATE_UNKNOWN"}],
        ),
        (
            {"error": "RemoteDisconnected"},
            [{"port": 8444, "handshake_error": "UNEXPECTED_EOF_WHILE_READING"}],
        ),
        (
            {"error": "RemoteDisconnected"},
            [
                {
                    "port": 8444,
                    "handshake_error": "SSLV3_ALERT_CERTIFICATE_UNKNOWN",
                    "path": "/anything",
                }
            ],
        ),
    ],
)
def test_native_certificate_refusal_rejects_incidental_or_unbound_failures(
    oracle, observed, events
):
    probe = importlib.import_module("openshell_https_probe")
    assert not probe.matches_native_https(observed, events, "certificate_refused", {"port": 8444})


def test_native_tls_refusal_needs_certificate_alert_without_http_side_effects(oracle):
    probe = importlib.import_module("openshell_https_probe")
    observed = {"error": "RemoteDisconnected"}
    alert = {"port": 8444, "handshake_error": "SSLV3_ALERT_CERTIFICATE_UNKNOWN"}
    assert probe.matches_native_https(observed, [alert], "certificate_refused", {"port": 8444})
    assert not probe.matches_native_https(
        observed, [alert, alert], "certificate_refused", {"port": 8444}
    )
    assert not probe.matches_native_https(
        observed, [alert, {"path": "/anything"}], "certificate_refused", {"port": 8444}
    )
    assert not probe.matches_native_https(
        {"status": 403, "body": '{"error":"policy_denied"}'}, [{"path": "/anything"}], 403, {}
    )


@pytest.mark.parametrize(
    "observed,events,blocked",
    [
        (
            {"error": "RemoteDisconnected"},
            [{"port": 8443, "handshake_error": "TLSV1_ALERT_UNKNOWN_CA"}],
            True,
        ),
        (
            {"error": "TimeoutError"},
            [{"port": 8443, "handshake_error": "TLSV1_ALERT_UNKNOWN_CA"}],
            False,
        ),
        ({"error": "RemoteDisconnected"}, [], False),
        (
            {"error": "RemoteDisconnected"},
            [{"port": 8444, "handshake_error": "TLSV1_ALERT_UNKNOWN_CA"}],
            False,
        ),
        (
            {"error": "RemoteDisconnected"},
            [{"port": 8443, "handshake_error": "SSLV3_ALERT_CERTIFICATE_EXPIRED"}],
            False,
        ),
        (
            {"error": "RemoteDisconnected"},
            [{"port": 8443, "handshake_error": "TLSV1_ALERT_UNKNOWN_CA"}, {"path": "/anything"}],
            False,
        ),
    ],
)
def test_missing_supervisor_trust_is_a_specific_finding_not_a_qualification_pass(
    oracle, observed, events, blocked
):
    probe = importlib.import_module("openshell_https_probe")
    request = {"port": 8443}
    assert probe.owned_trust_blocked(observed, events, request) is blocked
    assert not probe.matches_native_https(observed, events, 200, request)


@pytest.mark.parametrize("failure", ["RemoteDisconnected", "TimeoutError"])
def test_native_trust_finding_preserves_failed_qualification_and_unrun_cases(
    oracle, monkeypatch, tmp_path, failure
):
    import asyncio
    import hashlib
    import ssl
    from types import SimpleNamespace

    probe = importlib.import_module("openshell_https_probe")
    for name in ("ca", "valid", "hostname", "expired", "untrusted"):
        (tmp_path / (name + ".pem")).write_bytes(b"public fixture certificate")
    fingerprint = hashlib.sha256(b"public fixture certificate").hexdigest()
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("10.23.45.67", 8443))]
    )

    def control(host, path, *, method="GET", port=443, context=None):
        code = None
        if context is None or port == 8446:
            code = 20
        elif port == 8445:
            code = 10
        elif port == 8444 and host.startswith("allowed."):
            code = 62
        if code is not None:
            error = ssl.SSLCertVerificationError(1, "fixture verification refusal")
            error.verify_code = code
            raise error
        return {"method": method, "path": path, "host": host + ":" + str(port), "port": port}

    monkeypatch.setattr(
        probe,
        "native_https_policy",
        lambda *args, closed: SimpleNamespace(network_policies=not closed),
    )
    monkeypatch.setattr(probe, "control_https", control)
    events = []
    monkeypatch.setattr(probe, "origin_get", lambda *args: list(events))
    monkeypatch.setattr(probe, "guest_request", lambda url, method: "request")
    disposed = []
    created = []

    class Sandbox:
        def __init__(self, closed):
            self.closed = closed
            self.owner = {"closed": closed}

        async def exec(self, command, **kwargs):
            if command[-1] != "request":
                body = {
                    "installed_ca_sha256": fingerprint,
                    "system_bundle_contains_ca": True,
                    "effective_bundle_contains_ca": False,
                }
            elif self.closed:
                body = {"errno": 13}
            else:
                events.append({"port": 8443, "handshake_error": "TLSV1_ALERT_UNKNOWN_CA"})
                body = {"error": failure}
            return SimpleNamespace(exit_code=0, stderr_bytes=b"", stdout=json.dumps(body))

    async def acquire(*, policy):
        sandbox = Sandbox(not policy.network_policies)
        created.append(sandbox)
        return sandbox

    async def dispose(owner):
        disposed.append(owner)

    fixture = SimpleNamespace(
        report={},
        client=SimpleNamespace(image="fixture"),
        save=lambda: None,
        acquire=acquire,
        dispose=dispose,
    )
    if failure == "TimeoutError":
        with pytest.raises(RuntimeError, match="unexpected native HTTPS"):
            asyncio.run(probe.measure_native_https(fixture, "10.23.45.67", tmp_path / "ca.pem"))
        assert "blocked_by" not in fixture.report["https"]
        assert len(created) == len(disposed) == 1
    else:
        asyncio.run(probe.measure_native_https(fixture, "10.23.45.67", tmp_path / "ca.pem"))
        report = fixture.report["https"]
        assert report["qualification_passed"] is False
        assert [row["passed"] for row in report["cases"]] == [False, True]
        assert report["cases"][0]["finding"]
        assert len(report["unrun_cases"]) == 6
        assert "wrong_hostname" in report["unrun_cases"]
        assert len(created) == len(disposed) == 2
