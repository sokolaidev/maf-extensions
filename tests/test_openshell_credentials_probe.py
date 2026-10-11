"""Credential delivery needs exact origin evidence; unrelated failures never prove confinement."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

REQUEST = {
    "method": "GET",
    "path": "/provider/ok?case=fixture",
    "host": "origin.example:8000",
    "port": 8000,
    "authorization_sha256": "synthetic-digest",
}


@pytest.fixture
def oracle(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_credentials_probe").credential_observation


@pytest.mark.parametrize(
    "change",
    [
        {"authorization_sha256": None},
        {"authorization_sha256": "placeholder-digest"},
        {"path": "/provider/ok?case=other"},
        {"host": "other.example:8000"},
        {"port": 8001},
    ],
)
def test_rewrite_requires_exact_credential_and_endpoint_attribution(oracle, change):
    wrong = dict(REQUEST, **change)
    assert not oracle({"status": 200, "body": json.dumps(wrong)}, [wrong], 200, REQUEST)


@pytest.mark.parametrize(
    "observed",
    [
        {"error": "TimeoutError"},
        {"status": 403, "body": "upstream denied"},
        {"status": 403, "body": '{"error":"policy_denied"}'},
        {"status": 500, "body": '{"error":"credential_endpoint_mismatch"}'},
    ],
)
def test_binding_refusal_needs_its_own_error_and_status(oracle, observed):
    assert not oracle(observed, [], "credential_endpoint_mismatch", REQUEST)


@pytest.mark.parametrize(
    "error,status",
    [
        ("credential_endpoint_mismatch", 403),
        ("credential_unavailable", 500),
        ("policy_denied", 403),
    ],
)
def test_refusal_requires_no_correlated_origin_effect(oracle, error, status):
    observed = {"status": status, "body": json.dumps({"error": error})}
    assert oracle(observed, [], error, REQUEST)
    assert not oracle(observed, [REQUEST], error, REQUEST)


def test_success_requires_origin_echo_and_single_event(oracle):
    observed = {"status": 200, "body": json.dumps(REQUEST)}
    assert oracle(observed, [REQUEST], 200, REQUEST)
    assert not oracle(observed, [], 200, REQUEST)
    assert not oracle(observed, [REQUEST, REQUEST], 200, REQUEST)
    assert not oracle({"status": 200, "body": "{}"}, [REQUEST], 200, REQUEST)
