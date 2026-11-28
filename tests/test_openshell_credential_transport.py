"""Check TLS keep-alive, the origin response barrier and attribution independently."""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_credential_transport")


@pytest.fixture
def tls_origin(probe, monkeypatch, tmp_path):
    https = importlib.import_module("openshell_https_probe")
    tls_dir = tmp_path / "tls"
    https.prepare_native_tls(tls_dir, "10.23.45.67")
    host = "allowed.10.23.45.67.nip.io"
    original = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda name, *args: original("127.0.0.1" if name == host else name, *args),
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_dir / "ca.pem"))
    return https, tls_dir, host


def test_owned_tls_holds_one_response_while_a_separate_socket_stays_idle(probe, tls_origin):
    https, tls_dir, host = tls_origin
    gate = https.HeldTLSResponse()
    servers, events = https.native_tls_servers(tls_dir, ports=(0, 0, 0, 0), held_response=gate)
    port = servers[0].server_port
    transport = probe.GuestHTTPS()
    get = transport.get
    marker = "a" * 32
    url = f"https://{host}:{port}/provider/first?case={marker}&transport="
    try:
        prime = get("prime", url + "prime", {})
        assert prime["status"] == 200 and prime["kept_open"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(get, "hold", url + "held", {})
            try:
                deadline = time.monotonic() + 5
                while len(events) < 2 and not pending.done() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert len(events) == 2
                assert not gate.release("wrong")
                # Keep the idle TLS connection unused beyond the handshake timeout.
                with pytest.raises(TimeoutError):
                    pending.result(timeout=https.NATIVE_TLS_SOCKET_TIMEOUT + 0.5)
                assert not pending.done()
            finally:
                gate.release(marker)
            assert json.loads(pending.result(timeout=5)["body"]) == events[1]
        assert json.loads(get("reuse", url + "reuse", {})["body"]) == events[2]
        assert events[0]["origin_connection"] == events[2]["origin_connection"]
        assert events[0]["origin_connection"] != events[1]["origin_connection"]
    finally:
        if transport.idle_connection is not None:
            transport.idle_connection.close()
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize("mode", ["announced_close", "idle_eof", "truncated", "oversize"])
def test_real_guest_connection_faults(probe, tls_origin, mode):
    _, tls_dir, host = tls_origin
    requests = []
    accepted = []
    closed = threading.Event()

    class Server(HTTPServer):
        def get_request(self):
            sock, address = super().get_request()
            accepted.append(address)
            return sock, address

        def shutdown_request(self, request):
            super().shutdown_request(request)
            closed.set()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requests.append(self.path)
            followup = len(requests) == 2
            truncated = followup and mode == "truncated"
            body = b"x" * 4097 if followup and mode == "oversize" else b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + int(truncated)))
            if mode == "announced_close":
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = mode in ("announced_close", "idle_eof") or followup

        def log_message(self, format, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(tls_dir / "valid.pem", tls_dir / "valid.key")
    transport = probe.GuestHTTPS()
    with Server(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"https://{host}:{server.server_port}/provider/first"
        try:
            first = transport.get("prime", url + "?prime", {})
            assert first == {"status": 200, "body": "{}", "kept_open": mode != "announced_close"}
            if mode in ("announced_close", "idle_eof"):
                assert closed.wait(5)
            if mode == "oversize":
                with pytest.raises(ValueError, match="4096"):
                    transport.get("reuse", url + "?reuse", {})
            else:
                result = transport.get("reuse", url + "?reuse", {})
                if mode == "announced_close":
                    assert result == {"closed_before_request": True}
                elif mode == "idle_eof":
                    assert (
                        probe.transport_outcome(result, None, followup=True)
                        == "closed_without_origin_receipt"
                    )
                else:
                    assert result == {"closed_during_request": "IncompleteRead"}
                    assert (
                        probe.transport_outcome(result, {"path": requests[-1]}, followup=True)
                        == "closed_after_origin_receipt"
                    )
        finally:
            if transport.idle_connection is not None:
                transport.idle_connection.close()
            server.shutdown()
            thread.join(5)
    assert len(accepted) == 1
    expected = ["/provider/first?prime"]
    if mode in ("truncated", "oversize"):
        expected.append("/provider/first?reuse")
    assert requests == expected


@pytest.mark.parametrize(
    "mode",
    [
        "reused",
        "different_upstream",
        "closed",
        "early_close",
        "lost_response",
        "bad_receipt",
        "hold_write_failed",
        "edit_failed",
        "expired_hold",
        "extra_event",
    ],
)
def test_edit_is_bracketed_by_receipt_and_release(probe, monkeypatch, mode):
    order, events, rows = [], [], []
    current = {}
    first_id, other_id = "a" * 32, "b" * 32
    host, path = "owned.test", "/provider/first"

    class Guest:
        async def command(self, *, operation, url, source):
            assert source == "edited"
            target = urlsplit(url)
            marker = parse_qs(target.query)["case"][0]
            if current:
                assert marker == current["marker"]
            row = {
                "method": "GET",
                "path": target.path + "?" + target.query,
                "host": host + ":8443",
                "port": 8443,
                "authorization_sha256": "issuer",
                "origin_connection": other_id
                if operation == "hold" or mode == "different_upstream" and operation == "reuse"
                else first_id,
            }
            current.update(marker=marker, operation=operation, row=row)
            order.append(operation)
            if operation == "hold" and mode == "bad_receipt":
                row["authorization_sha256"] = "other"
            if operation != "reuse" or mode not in ("closed", "early_close"):
                events.append(row)
            if operation == "hold" and mode == "hold_write_failed":
                raise OSError("control stream close failed")
            if operation == "reuse" and mode == "extra_event":
                events.append(dict(row))

        async def request_result(self, source):
            op = current["operation"]
            if op == "hold":
                assert "release" in order
            if op == "reuse" and mode in ("closed", "early_close"):
                return {"guest": {"closed_before_request": True}}
            if op == "hold" and mode == "lost_response":
                return {"guest": {"closed_during_request": "RemoteDisconnected"}}
            result = {"status": 200, "body": json.dumps(current["row"])}
            if op == "prime":
                result["kept_open"] = mode != "early_close"
            return {"guest": result}

    def edit(client, record):
        assert order == ["prime", "hold"]
        order.append("edit")
        if mode == "edit_failed":
            raise RuntimeError("write failed")

    def release(ip, path, *, method):
        assert path == "/__release_tls?case=" + current["marker"] and method == "POST"
        order.append("release")
        return {"released": mode != "expired_hold"}

    async def reconcile(*, expected=None):
        out = list(events)
        events.clear()
        if expected is not None:
            assert out == expected
        return out

    monkeypatch.setattr(probe, "origin_get", release)
    profile = NS(endpoint_path=path, update_description=edit)
    run = probe.measure_transport(
        Guest(),
        profile,
        None,
        {"authorization_sha256": "issuer"},
        "127.0.0.1",
        host,
        reconcile,
        lambda group, row: rows.append(row),
    )
    if mode == "hold_write_failed":
        with pytest.raises(OSError, match="control stream close failed"):
            asyncio.run(run)
        assert order == ["prime", "hold", "release"]
        assert rows[0]["origin_released"] and not rows[0]["complete"]
        return
    if mode in ("bad_receipt", "edit_failed", "expired_hold", "extra_event"):
        with pytest.raises(RuntimeError):
            asyncio.run(run)
        assert "release" in order
        assert not rows[0]["complete"]
        if mode == "bad_receipt":
            assert "edit" not in order
        return
    asyncio.run(run)
    assert order == ["prime", "hold", "edit", "release", "reuse"]
    (row,) = rows
    assert row["complete"] and row["receipt_observed_ns"] <= row["exact_readback_ns"]
    assert row["in_flight"] == (
        "closed_after_origin_receipt" if mode == "lost_response" else "response_received"
    )
    if mode in ("closed", "early_close"):
        assert row["followup"] == "socket_closed_before_request"
        assert row["same_origin_connection"] is None
    else:
        assert row["followup"] == "response_received"
        assert row["same_origin_connection"] == (mode != "different_upstream")
