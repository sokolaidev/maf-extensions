"""The SSH bridge must bind the original sandbox and close after handshake failures."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_sftp_probe")


def test_wrong_session_identity_refuses_before_opening_tunnel(probe, monkeypatch):
    monkeypatch.setattr(probe, "import_module", lambda _: None)
    client = SimpleNamespace(
        rpc=lambda *args: SimpleNamespace(sandbox_id="replacement"),
        scope=object(),
        pb=SimpleNamespace(CreateSshSessionRequest=lambda **kwargs: kwargs),
    )
    with pytest.raises(RuntimeError, match="another physical sandbox"):
        with probe.native_sftp(client, "name", "original"):
            pytest.fail("unexpected session")


@pytest.mark.parametrize("stage", ["handshake", "host_key", "authentication"])
def test_failed_ssh_setup_closes_relay_and_sockets(probe, monkeypatch, stage):
    closed = []
    captured = {}

    class Call:
        def __iter__(self):
            return iter(())

        def cancel(self):
            closed.append("call")

    class Transport:
        def __init__(self, sock):
            captured["socket"] = sock

        def start_client(self, timeout):
            if stage == "handshake":
                raise RuntimeError("handshake")

        def get_remote_server_key(self):
            return SimpleNamespace(asbytes=lambda: b"fixture-key")

        def auth_none(self, user):
            raise RuntimeError("authentication")

        def close(self):
            closed.append("transport")

    def forward(requests, **kwargs):
        captured["init"] = next(requests).init
        captured["timeout"] = kwargs["timeout"]
        return Call()

    monkeypatch.setattr(probe, "import_module", lambda _: SimpleNamespace(Transport=Transport))
    client = SimpleNamespace(
        scope=SimpleNamespace(workspace="workspace"),
        token="caller-token",
        rpc=lambda *args: SimpleNamespace(
            sandbox_id="original",
            token="session-token",
            host_key_fingerprint="wrong" if stage == "host_key" else "",
        ),
        pb=SimpleNamespace(
            CreateSshSessionRequest=lambda **kwargs: kwargs,
            TcpForwardFrame=lambda **kwargs: SimpleNamespace(**kwargs),
            TcpForwardInit=lambda **kwargs: SimpleNamespace(**kwargs),
            SshRelayTarget=lambda: object(),
        ),
        stub=SimpleNamespace(ForwardTcp=forward),
    )
    with pytest.raises(RuntimeError, match="host key mismatch" if stage == "host_key" else stage):
        with probe.native_sftp(client, "name", "original"):
            pytest.fail("unexpected successful handshake")
    assert closed == ["transport", "call"]
    assert captured["socket"].fileno() == -1
    assert captured["init"].sandbox == "name"
    assert captured["init"].workspace == "workspace"
    assert captured["init"].authorization_token == "session-token"
    assert captured["timeout"] == 60
