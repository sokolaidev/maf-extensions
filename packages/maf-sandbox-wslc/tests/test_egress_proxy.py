"""The filtering CONNECT proxy that turns this backend's egress from CLOSED into ALLOWLIST.

These tests run the proxy in-process against real sockets on the loopback interface — no
wslc, no containers. What the container adds is placement, not behaviour: the same server
listens dual-homed there, and `test_wslc_e2e.py` covers that half.
"""

from __future__ import annotations

import asyncio

from maf_sandbox_wslc._proxy import build_context
from maf_sandbox_wslc._proxy.proxy import ProxyServer, host_allowed


class TestHostAllowed:
    def test_an_exact_name_matches(self):
        assert host_allowed("mcr.microsoft.com", ("mcr.microsoft.com",))

    def test_matching_ignores_case(self):
        assert host_allowed("MCR.Microsoft.COM", ("mcr.microsoft.com",))
        assert host_allowed("mcr.microsoft.com", ("MCR.MICROSOFT.COM",))

    def test_a_wildcard_matches_a_subdomain(self):
        assert host_allowed("eastus.data.mcr.microsoft.com", ("*.data.mcr.microsoft.com",))

    def test_a_wildcard_does_not_match_the_bare_domain(self):
        assert not host_allowed("data.mcr.microsoft.com", ("*.data.mcr.microsoft.com",))

    def test_an_unlisted_host_is_denied(self):
        assert not host_allowed("pypi.org", ("mcr.microsoft.com", "*.data.mcr.microsoft.com"))

    def test_an_empty_allowlist_denies_everything(self):
        assert not host_allowed("mcr.microsoft.com", ())

    def test_a_listed_name_does_not_match_its_own_subdomains(self):
        assert not host_allowed("evil.mcr.microsoft.com", ("mcr.microsoft.com",))


async def _echo_server() -> tuple[asyncio.Server, int]:
    """A loopback TCP server that echoes whatever it receives — the tunnel's far end."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _request(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await reader.read(4096)
    writer.close()
    return data


class TestConnectProxy:
    def test_an_allowed_connect_tunnels_bytes_both_ways(self):
        async def scenario() -> None:
            echo, echo_port = await _echo_server()
            proxy = ProxyServer(("127.0.0.1",), ports=(echo_port,))
            await proxy.start()
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
            writer.write(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            status = await reader.readuntil(b"\r\n\r\n")
            assert b"200" in status
            writer.write(b"ping")
            await writer.drain()
            assert await reader.read(1024) == b"echo:ping"
            writer.close()
            await proxy.aclose()
            echo.close()

        asyncio.run(scenario())

    def test_a_denied_host_gets_403_and_no_connection(self):
        async def scenario() -> None:
            proxy = ProxyServer(("allowed.example",), ports=(443,))
            await proxy.start()
            reply = await _request(proxy.bound_port, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
            assert reply.startswith(b"HTTP/1.1 403")
            await proxy.aclose()

        asyncio.run(scenario())

    def test_an_allowed_host_on_a_denied_port_gets_403(self):
        async def scenario() -> None:
            proxy = ProxyServer(("allowed.example",), ports=(443,))
            await proxy.start()
            reply = await _request(proxy.bound_port, b"CONNECT allowed.example:22 HTTP/1.1\r\n\r\n")
            assert reply.startswith(b"HTTP/1.1 403")
            await proxy.aclose()

        asyncio.run(scenario())

    def test_anything_but_connect_gets_405(self):
        async def scenario() -> None:
            proxy = ProxyServer(("allowed.example",), ports=(443,))
            await proxy.start()
            reply = await _request(
                proxy.bound_port, b"GET http://allowed.example/ HTTP/1.1\r\n\r\n"
            )
            assert reply.startswith(b"HTTP/1.1 405")
            await proxy.aclose()

        asyncio.run(scenario())

    def test_a_malformed_request_gets_400(self):
        async def scenario() -> None:
            proxy = ProxyServer(("allowed.example",), ports=(443,))
            await proxy.start()
            reply = await _request(proxy.bound_port, b"not-a-request\r\n\r\n")
            assert reply.startswith(b"HTTP/1.1 400")
            await proxy.aclose()

        asyncio.run(scenario())

    def test_an_unreachable_target_gets_502(self):
        async def scenario() -> None:
            # Port 1 on loopback: nothing listens there, so the dial fails fast.
            proxy = ProxyServer(("127.0.0.1",), ports=(1,))
            await proxy.start()
            reply = await _request(proxy.bound_port, b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
            assert reply.startswith(b"HTTP/1.1 502")
            await proxy.aclose()

        asyncio.run(scenario())


class TestBuildContext:
    def test_the_packaged_context_carries_the_dockerfile_and_the_script(self):
        context = build_context()
        assert (context / "Dockerfile").is_file()
        assert (context / "proxy.py").is_file()

    def test_the_dockerfile_pins_its_base_and_copies_the_script(self):
        dockerfile = (build_context() / "Dockerfile").read_text(encoding="utf-8")
        assert "mcr.microsoft.com/azurelinux/base/core:3.0" in dockerfile
        assert "COPY proxy.py" in dockerfile
