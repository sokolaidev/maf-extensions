"""Check TLS keep-alive, the origin response barrier and attribution independently."""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_credential_transport")


def test_owned_tls_holds_one_response_while_a_separate_socket_stays_idle(
    probe, monkeypatch, tmp_path
):
    https = importlib.import_module("openshell_https_probe")
    https.prepare_native_tls(tmp_path, "10.23.45.67")
    gate = https.HeldTLSResponse()
    servers, events = https.native_tls_servers(tmp_path, ports=(0, 0, 0, 0), held_response=gate)
    port = servers[0].server_port
    host = "allowed.10.23.45.67.nip.io"
    original = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda name, *args: original("127.0.0.1" if name == host else name, *args),
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    namespace: dict = {"ssl": ssl}
    exec(probe.GUEST_HTTPS, namespace)
    get = namespace["transport_get"]
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
                assert not pending.done()
                assert not gate.release("wrong")
            finally:
                gate.release(marker)
            assert json.loads(pending.result(timeout=5)["body"]) == events[1]
        assert json.loads(get("reuse", url + "reuse", {})["body"]) == events[2]
        assert events[0]["origin_connection"] == events[2]["origin_connection"]
        assert events[0]["origin_connection"] != events[1]["origin_connection"]
    finally:
        if namespace["idle_connection"] is not None:
            namespace["idle_connection"].close()
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize(
    "mode",
    [
        "reused",
        "different_upstream",
        "closed",
        "early_close",
        "lost_response",
        "bad_receipt",
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
