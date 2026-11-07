"""Native egress admission and host/guest trust separation."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from maf_sandbox import Capability, Egress, EgressRule, HttpMethod, SandboxSpec

from maf_sandbox_bubblewrap import BubblewrapSandboxBackend, BubblewrapSandboxConfig
from maf_sandbox_bubblewrap._backend import _arguments
from maf_sandbox_bubblewrap._egress import Proxy, _owns_listener, policy


def test_allowlist_requires_explicit_proxy_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    config = BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path)
    closed = BubblewrapSandboxBackend(config)
    assert closed.declarations.egress_modes == {Egress.CLOSED}
    assert Capability.EGRESS_METHODS not in closed.declarations.capabilities
    enabled = BubblewrapSandboxBackend(
        replace(config, egress_proxy=tmp_path / "proxy", egress_proxy_sha256="a" * 64)
    )
    assert Egress.ALLOWLIST in enabled.declarations.egress_modes
    assert Capability.EGRESS_METHODS in enabled.declarations.capabilities
    assert Capability.EGRESS_PATHS in enabled.declarations.capabilities


@pytest.mark.parametrize(
    "changes",
    [
        {"egress_proxy": Path("/proxy")},
        {"egress_proxy_sha256": "a" * 64},
        {"egress_proxy": Path("proxy"), "egress_proxy_sha256": "a" * 64},
        {"egress_proxy": Path("/proxy"), "egress_proxy_sha256": "not-a-digest"},
    ],
)
def test_incomplete_proxy_identity_is_refused(tmp_path, changes):
    with pytest.raises(ValueError):
        BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path, **changes)


def test_native_policy_preserves_host_methods_paths_and_denies_host_addresses(tmp_path):
    spec = SandboxSpec(
        kind="pricing",
        egress=Egress.ALLOWLIST,
        egress_allow=(
            EgressRule("prices.azure.com", methods=(HttpMethod.GET,), paths=("/api/retail/*",)),
        ),
    )
    result = policy(spec, tmp_path)
    assert result["transforms"] == [
        {
            "name": "allowlist",
            "config": {
                "domains": [],
                "rules": [
                    {"host": "prices.azure.com", "methods": ["GET"], "paths": ["/api/retail/*"]},
                ],
            },
        }
    ]
    proxy = result["proxy"]
    tls = result["tls"]
    assert isinstance(proxy, dict) and isinstance(tls, dict)
    assert "127.0.0.0/8" in proxy["upstream_deny_cidrs"]
    assert "169.254.0.0/16" in proxy["upstream_deny_cidrs"]
    assert tls["mode"] == "mitm"
    assert proxy["http_listen"] == proxy["https_listen"] == "127.0.0.1:0"
    assert result["metrics"] == {"listen": "127.0.0.1:0"}
    assert result["log"] == {"level": "info"}


@pytest.mark.parametrize(
    ("address", "state", "inode", "owned"),
    [
        ("0100007F:ABCD", "0A", "123", True),
        ("0100007F:ABCD", "0A", "456", False),
        ("0100007F:ABCD", "01", "123", False),
        ("00000000:ABCD", "0A", "123", False),
        ("0100007F:ABCE", "0A", "123", False),
    ],
)
def test_listener_requires_child_owned_loopback_socket(
    tmp_path, monkeypatch, address, state, inode, owned
):
    process = tmp_path / "42"
    (process / "fd").mkdir(parents=True)
    (process / "fd/3").touch()
    (process / "net").mkdir()
    (process / "net/tcp").write_text(
        f"0: {address} 00000000:0000 {state} 0:0 0:0 0 1000 0 {inode}\n", encoding="ascii"
    )
    monkeypatch.setattr("maf_sandbox_bubblewrap._egress.os.readlink", lambda _: "socket:[123]")
    assert _owns_listener(42, 0xABCD, tmp_path) is owned
    assert not _owns_listener(43, 0xABCD, tmp_path)


@pytest.mark.parametrize("address", ["127.0.0.1:0", "127.0.0.1:65536", "0.0.0.0:123", None])
def test_proxy_rejects_invalid_startup_addresses(tmp_path, address):
    proxy = Proxy(tmp_path)
    proxy._read_ready(
        bytearray(json.dumps({"msg": "tunnel proxy starting", "addr": address}).encode())
    )
    assert proxy.tunnel_port is None


def test_proxy_reads_fragmented_startup_record_and_bounds_diagnostics(tmp_path):
    async def scenario():
        proxy = Proxy(tmp_path)
        output = asyncio.StreamReader()
        proxy.process = Mock(stdout=output)
        drain = asyncio.create_task(proxy._drain())
        record = b'{"msg":"tunnel proxy starting","addr":"127.0.0.1:12345"}\n'
        output.feed_data(b"contract ready\n" + b"x" * 8193 + record)
        await asyncio.sleep(0)
        assert proxy.tunnel_port is None
        output.feed_data(record[:19])
        await asyncio.sleep(0)
        assert proxy.tunnel_port is None
        output.feed_data(record[19:])
        output.feed_eof()
        await drain
        assert proxy.tunnel_port == 12345
        assert len(proxy.error) == 8192

    asyncio.run(scenario())


def test_unrelated_listener_receives_no_guest_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr("maf_sandbox_bubblewrap._egress._owns_listener", lambda *_: False)

    async def scenario():
        received = asyncio.get_running_loop().create_future()

        async def unrelated(reader, writer):
            received.set_result(await reader.read())
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(unrelated, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            proxy = Proxy(tmp_path)
            proxy.process = Mock(pid=42, returncode=None)
            guest = asyncio.StreamReader()
            guest.feed_data(b"guest request must not escape")
            guest.feed_eof()
            writer = Mock()
            await proxy._relay(guest, writer, port)
            assert await asyncio.wait_for(received, 5) == b""
            writer.close.assert_called_once()

    asyncio.run(scenario())


def test_only_public_proxy_files_enter_the_guest(tmp_path):
    config = BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path)
    proxy = Proxy(tmp_path / "private")
    args = _arguments(config, proxy)
    assert "--unshare-net" in args
    assert "--share-net" not in args
    assert str(proxy.directory / "public") in args
    assert str(proxy.directory) not in args
    assert not any("ca.key" in arg for arg in args)
    assert "SSL_CERT_FILE" in args
    assert "HTTPS_PROXY" not in _arguments(config)


def test_proxy_digest_mismatch_precedes_any_process_or_state(tmp_path):
    binary = tmp_path / "proxy"
    binary.write_bytes(b"incorrect executable")
    proxy = Proxy(tmp_path / "state")
    with pytest.raises(ValueError, match="SHA-256"):
        asyncio.run(proxy.start(binary, "0" * 64, SandboxSpec(kind="pricing"), tmp_path))
    assert not proxy.directory.exists()
    assert proxy.process is None
