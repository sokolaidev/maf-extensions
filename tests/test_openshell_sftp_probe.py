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


@pytest.mark.parametrize(
    "stage",
    [
        "forward",
        "thread",
        "transport",
        "handshake",
        "host_key",
        "authentication",
        "open_sftp",
        "channel",
        "close",
    ],
)
def test_failed_ssh_setup_closes_relay_and_sockets(probe, monkeypatch, stage):
    closed = []
    captured = {}
    original_pair = probe.socket.socketpair

    def socketpair():
        pair = original_pair()
        captured["sockets"] = pair
        return pair

    monkeypatch.setattr(probe.socket, "socketpair", socketpair)
    if stage == "thread":

        def failed_start(self):
            raise RuntimeError("thread")

        monkeypatch.setattr(probe.threading.Thread, "start", failed_start)

    class Call:
        def __iter__(self):
            return iter(())

        def cancel(self):
            closed.append("call")

    class Transport:
        def __init__(self, sock):
            captured["socket"] = sock
            if stage == "transport":
                raise RuntimeError("transport")

        def start_client(self, timeout):
            if stage == "handshake":
                raise RuntimeError("handshake")

        def get_remote_server_key(self):
            return SimpleNamespace(asbytes=lambda: b"fixture-key")

        def auth_none(self, user):
            if stage == "authentication":
                raise RuntimeError("authentication")

        def open_sftp_client(self):
            if stage == "open_sftp":
                raise RuntimeError("open_sftp")
            return Sftp()

        def close(self):
            closed.append("transport")

    class Sftp:
        def get_channel(self):
            if stage == "channel":
                raise RuntimeError("channel")
            return SimpleNamespace(settimeout=lambda seconds: None)

        def close(self):
            closed.append("sftp")
            if stage == "close":
                raise RuntimeError("close")

    def forward(requests, **kwargs):
        if stage == "forward":
            raise RuntimeError("forward")
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
            TcpForwardFrame=SimpleNamespace,
            TcpForwardInit=SimpleNamespace,
            SshRelayTarget=object,
        ),
        stub=SimpleNamespace(ForwardTcp=forward),
    )
    with pytest.raises(RuntimeError, match="host key mismatch" if stage == "host_key" else stage):
        with probe.native_sftp(client, "name", "original"):
            if stage != "close":
                pytest.fail("unexpected successful handshake")
    expected = [] if stage == "forward" else ["call"]
    if stage not in ("forward", "thread", "transport"):
        expected.insert(0, "transport")
    if stage in ("channel", "close"):
        expected.insert(0, "sftp")
    assert closed == expected
    assert all(sock.fileno() == -1 for sock in captured["sockets"])
    if stage == "forward":
        return
    assert captured["init"].sandbox == "name"
    assert captured["init"].workspace == "workspace"
    assert captured["init"].authorization_token == "session-token"
    assert captured["timeout"] == 60
