"""Refresh evidence must bind the owned credential and exclude synthetic secret values."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_refresh_probe")


def status_fixture():
    return NS(provider="provider", provider_id="physical-id", credential_key="TOKEN", strategy=4)


@pytest.mark.parametrize("count", [0, 2])
def test_ambiguous_refresh_status_is_refused(probe, count):
    with pytest.raises(RuntimeError):
        probe.select_refresh_status(
            NS(credentials=[status_fixture()] * count), "provider", "physical-id", "TOKEN", 4
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "other"),
        ("provider_id", "replacement"),
        ("credential_key", "OTHER"),
        ("strategy", 1),
    ],
)
def test_unrelated_status_never_proves_refresh(probe, field, value):
    status = status_fixture()
    setattr(status, field, value)
    with pytest.raises(RuntimeError):
        probe.select_refresh_status(NS(credentials=[status]), "provider", "physical-id", "TOKEN", 4)


def test_exact_credential_status_is_selected(probe):
    status = status_fixture()
    assert (
        probe.select_refresh_status(NS(credentials=[status]), "provider", "physical-id", "TOKEN", 4)
        is status
    )


def request_token(issuer, secret=None):
    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": "spike1797",
            "client_secret": secret or issuer.client_secret,
        }
    ).encode()
    with build_opener(ProxyHandler({})).open(Request(issuer.url, data=body), timeout=3) as response:
        return json.load(response)


def test_issuer_rotates_tokens_but_records_only_digests(probe, capsys):
    with probe.TokenIssuer() as issuer:
        first = request_token(issuer)
        second = request_token(issuer)
        assert first["access_token"] != second["access_token"]
        assert issuer.snapshot()["generation"] == 2
        assert (
            issuer.snapshot()["authorization_sha256"]
            == hashlib.sha256(("Bearer " + second["access_token"]).encode()).hexdigest()
        )
        assert all(event["valid_material"] for event in issuer.events)
    evidence = json.dumps(issuer.events) + capsys.readouterr().out
    for secret in (issuer.client_secret, first["access_token"], second["access_token"]):
        assert secret not in evidence


@pytest.mark.parametrize("mode,code", [("temporarily_unavailable", 503), ("invalid_client", 400)])
def test_failed_refresh_never_changes_the_last_minted_value(probe, mode, code):
    with probe.TokenIssuer() as issuer:
        request_token(issuer)
        before = issuer.snapshot()
        issuer.configure(mode=mode)
        with pytest.raises(HTTPError) as exc:
            request_token(issuer)
        assert exc.value.code == code
        assert json.load(exc.value) == {"error": mode}
        assert issuer.snapshot() == before
        assert issuer.events[-1]["mode"] == mode


def test_wrong_client_material_never_mints_a_token(probe):
    with probe.TokenIssuer() as issuer:
        with pytest.raises(HTTPError) as exc:
            request_token(issuer, "wrong-secret")
        assert exc.value.code == 400
        assert issuer.snapshot() == {}
        assert issuer.events[-1]["valid_material"] is False
