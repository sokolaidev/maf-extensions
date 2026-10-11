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
