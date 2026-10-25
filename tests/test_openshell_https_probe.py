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
