"""Offline import tests using the SDK's request serialization and polling."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.containerapps.sandbox.aio import SandboxGroupClient

_SPEC = importlib.util.spec_from_file_location(
    "import_disk_image", Path(__file__).resolve().parents[1] / "scripts" / "import_disk_image.py"
)
assert _SPEC and _SPEC.loader
importer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(importer)

_REFERENCE = "acr.azurecr.io/bicep-sandbox:0.46.1-2"
_ARGS = [
    "--endpoint",
    "https://management.example.azuredevcompute.io",
    "--subscription",
    "subscription",
    "--resource-group",
    "resource-group",
    "--group",
    "group",
    "--image",
    _REFERENCE,
]
_TOKEN = "test-registry-token"


@pytest.fixture
def sdk(monkeypatch):
    credential = SimpleNamespace(close=AsyncMock())
    client = SandboxGroupClient(
        "https://management.example.azuredevcompute.io",
        credential,
        subscription_id="subscription",
        resource_group="resource-group",
        sandbox_group="group",
    )
    inventory = []
    request = AsyncMock(return_value=SimpleNamespace(json=lambda: {"value": inventory}))
    created = {"id": "img-new", "image": {"base": _REFERENCE}, "status": {"state": "Ready"}}
    put = AsyncMock(return_value=created)
    monkeypatch.setattr(client, "_send_request", request)
    monkeypatch.setattr(client, "_dp_put", put)
    monkeypatch.setattr(client, "close", AsyncMock())
    monkeypatch.setattr("azure.identity.aio.DefaultAzureCredential", lambda: credential)
    monkeypatch.setattr("azure.containerapps.sandbox.aio.SandboxGroupClient", lambda **_: client)
    return SimpleNamespace(
        client=client,
        credential=credential,
        inventory=inventory,
        put=put,
        request=request,
        created=created,
    )


@pytest.mark.parametrize("stdin", [False, True])
def test_registry_credentials_reach_the_sdk_request(sdk, monkeypatch, capsys, stdin):
    sdk.inventory.append(
        {"id": "img-old", "image": {"base": "acr.azurecr.io/bicep-sandbox:0.46.1-1"}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_TOKEN + "\n"))
    token_args = ["--token-stdin"] if stdin else ["--token", _TOKEN]
    assert importer.main([*_ARGS, "--username", "registry-user", *token_args]) == 0
    body = sdk.put.await_args.args[1]
    assert body == {
        "image": {"base": _REFERENCE},
        "labels": {"name": "bicep-sandbox-0.46.1-2"},
        "registryCredentials": {"username": "registry-user", "token": _TOKEN},
    }
    captured = capsys.readouterr()
    assert captured.out == "img-new\n"
    assert _TOKEN not in captured.out + captured.err
    sdk.client.close.assert_awaited_once()
    sdk.credential.close.assert_awaited_once()


@pytest.mark.parametrize("auth", [[], ["--identity", "/identities/pull"]])
def test_public_and_managed_identity_imports(sdk, auth):
    assert importer.main([*_ARGS, "--name", "chosen-name", *auth]) == 0
    body = sdk.put.await_args.args[1]
    assert body["labels"] == {"name": "chosen-name"}
    assert "registryCredentials" not in body
    if auth:
        assert body["managedIdentityResourceId"] == "/identities/pull"
    else:
        assert "managedIdentityResourceId" not in body


@pytest.mark.parametrize("reference", [_REFERENCE, "acr.azurecr.io/bicep@sha256:" + "a" * 64])
def test_existing_reference_is_refused_without_an_import(sdk, capsys, reference):
    sdk.inventory.extend(
        [
            {"id": "img-other", "image": {"base": "acr.azurecr.io/other:1"}},
            {"id": "img-old", "image": {"base": reference}},
        ]
    )
    assert importer.main([*_ARGS[:-1], reference, "--name", "different-name"]) == 1
    sdk.put.assert_not_awaited()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Nothing imported" in captured.err
    assert "img-old" in captured.err
    assert "new build tag" in captured.err
    sdk.client.close.assert_awaited_once()
    sdk.credential.close.assert_awaited_once()


@pytest.mark.parametrize(
    "auth",
    [
        ["--username", "registry-user"],
        ["--token", _TOKEN],
        ["--token-stdin"],
        ["--username", "", "--token", _TOKEN],
        ["--username", "registry-user", "--token", " "],
        ["--username", "registry-user", "--token-stdin"],
        ["--identity", "/identities/pull", "--username", "registry-user", "--token", _TOKEN],
        ["--identity", "/identities/pull", "--token-stdin"],
        ["--username", "registry-user", "--token", _TOKEN, "--token-stdin"],
    ],
)
def test_invalid_authentication_is_rejected_before_contacting_azure(sdk, monkeypatch, capsys, auth):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit) as raised:
        importer.main([*_ARGS, *auth])
    assert raised.value.code == 2
    sdk.request.assert_not_awaited()
    sdk.put.assert_not_awaited()
    captured = capsys.readouterr()
    assert _TOKEN not in captured.out + captured.err


def test_failed_import_does_not_print_a_success_id(sdk, capsys):
    sdk.put.side_effect = RuntimeError("import failed")
    with pytest.raises(RuntimeError, match="import failed"):
        importer.main(_ARGS)
    assert capsys.readouterr().out == ""
    sdk.client.close.assert_awaited_once()
    sdk.credential.close.assert_awaited_once()


def test_credential_closes_even_when_client_close_fails(sdk):
    sdk.client.close.side_effect = RuntimeError("close failed")
    with pytest.raises(RuntimeError, match="close failed"):
        importer.main(_ARGS)
    sdk.credential.close.assert_awaited_once()
