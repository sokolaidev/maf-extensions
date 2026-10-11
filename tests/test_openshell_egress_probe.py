"""Network refusals require policy evidence and an independent, correctly attributed origin journal."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

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
